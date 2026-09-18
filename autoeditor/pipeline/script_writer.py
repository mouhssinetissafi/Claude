"""Normal mode script writer: topic (+ optional facts) -> script.json.

Lines carry a ``media_query`` that the media stage resolves against the local
library or a stock provider. The same editorial, fact-safety, originality and
duration rules as the footage-only shot planner apply.
"""

from __future__ import annotations

import json
import re
from typing import Any

from autoeditor.cache import JsonCache, make_key
from autoeditor.config import Config
from autoeditor.footage_only.shot_plan import estimate_seconds, find_banned_phrases, numeric_claims
from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.duration import expansion_instruction, policy_from_config
from autoeditor.pipeline.job import JobPaths, write_json
from autoeditor.pipeline.originality import variation_instruction
from autoeditor.providers.base import LLMProvider, ProviderError
from autoeditor.schemas import LLM_NORMAL_SCRIPT_SCHEMA, validate

log = get_logger(__name__)

PROMPT_VERSION = "v2"

SYSTEM_PROMPT = """You write narration for automated YouTube Shorts.
Rules: American English, short sentences, active voice, ~8th-grade readability, strong cold open,
escalation, a payoff, and a clean loopable ending. Land inside target_range_seconds at words_per_second;
never go below min_seconds, and reach the length with useful specific content, never filler.
Never use "Did you know", "In this video", "mind-blowing", "game changer", "insane" or generic hype.
Never claim personal experience ("I tested", "I used", "I recommend").
Only state facts that appear in the provided facts list. Any date, price, statistic, spec or causal
claim that is not in the facts list must be avoided or added to facts_to_verify - never invented.
ORIGINALITY: write an original narrative in your own framing; never reproduce, closely paraphrase or
lightly reword an article, press release, product page or another creator's script. Follow the
VARIATION PROFILE so the hook, structure and ending differ from other videos.
Each line needs a short media_query (2-5 words) describing stock footage that would illustrate it.
overlay_text is 1-3 CAPS words or null; emphasis_words must appear verbatim in the narration.
Return only the JSON object."""


def build_user_prompt(
    topic: str,
    facts: list[str],
    cfg: Config,
    *,
    variation: dict[str, Any] | None = None,
    expand: dict[str, Any] | None = None,
) -> str:
    policy = policy_from_config(cfg)
    payload: dict[str, Any] = {
        "topic": topic,
        "facts": facts,
        "target_range_seconds": [policy.target_min_seconds, policy.target_max_seconds],
        "min_seconds": policy.min_final_seconds,
        "hard_max_seconds": policy.hard_max_seconds,
        "words_per_second": policy.words_per_second,
        "variation": variation,
    }
    if expand:
        payload["expand"] = expand
    intro = variation_instruction(variation) + "\n" if variation else ""
    if expand:
        intro += expansion_instruction(float(expand["current_seconds"]), float(expand["add_seconds"]), policy) + "\n"
        return intro + "Revise and expand the existing script. Input JSON:\n" + json.dumps(payload, ensure_ascii=False)
    return intro + "Write the script. Input JSON:\n" + json.dumps(payload, ensure_ascii=False)


def read_facts(text: str | None) -> list[str]:
    if not text:
        return []
    return [re.sub(r"^[-*\d.)\s]+", "", ln).strip() for ln in text.splitlines() if ln.strip()]


def _call_llm(llm: LLMProvider, user: str, cfg: Config, cache: JsonCache | None) -> dict[str, Any]:
    banned = [str(b) for b in cfg.get("script.banned_phrases", [])]
    key = make_key("normal_script", getattr(llm, "name", "llm"), cfg.get("llm.model"), PROMPT_VERSION, user)
    cached = cache.get(key) if cache else None
    if cached:
        return dict(cached)
    feedback = ""
    attempt = 0
    while True:
        attempt += 1
        result = llm.complete_json(
            system=SYSTEM_PROMPT, user=user + feedback, output_schema=LLM_NORMAL_SCRIPT_SCHEMA, max_tokens=int(cfg.get("llm.max_tokens", 16000))
        )
        hits = find_banned_phrases(result.data.get("lines", []), banned)
        if hits and attempt <= int(cfg.get("llm.max_json_retries", 2)):
            feedback = "\n\nREVISION REQUIRED: forbidden phrases used: " + "; ".join(hits)
            continue
        if hits:
            raise ProviderError("script still contains banned phrases: " + "; ".join(hits))
        if cache:
            cache.put(key, result.data)
        return result.data


def _plan_to_script(
    plan: dict[str, Any], topic: str, facts: list[str], cfg: Config, paths: JobPaths, *, variation: dict[str, Any] | None, expansions: int
) -> dict[str, Any]:
    lines: list[dict[str, Any]] = []
    for line in plan["lines"]:
        narration = str(line["narration"]).strip()
        if not narration:
            continue
        lines.append(
            {
                "id": len(lines) + 1,
                "narration": narration,
                "media_query": str(line.get("media_query") or topic).strip(),
                "overlay_text": (str(line["overlay_text"]).strip()[:24] or None) if line.get("overlay_text") else None,
                "emphasis_words": [w for w in line.get("emphasis_words", []) if isinstance(w, str) and w.lower().strip(".,!?") in narration.lower()],
            }
        )
    if not lines:
        raise ProviderError("script writer returned no lines")
    policy = policy_from_config(cfg)
    while len(lines) > 3 and estimate_seconds(lines, policy.words_per_second) > policy.hard_max_seconds:
        lines.pop()
    facts_to_verify = [str(f) for f in plan.get("facts_to_verify", [])]
    for claim in numeric_claims(lines):
        if not facts and claim not in facts_to_verify:
            facts_to_verify.append(claim)
    script = {
        "version": 1,
        "mode": "normal",
        "title": str(plan["title"]).strip() or topic,
        "description": str(plan.get("description", "")).strip(),
        "topic": topic,
        "theme": str(cfg.get("render.theme", "default")),
        "facts_to_verify": facts_to_verify,
        "variation": variation,
        "expansions": expansions,
        "lines": lines,
    }
    validate(script, "script")
    write_json(paths.script_json, script)
    log.info("Script: %d lines, ~%.0fs estimated, %d expansion(s)", len(lines), estimate_seconds(lines, policy.words_per_second), expansions)
    return script


def write_script(
    topic: str,
    facts: list[str],
    llm: LLMProvider,
    cfg: Config,
    paths: JobPaths,
    *,
    cache: JsonCache | None = None,
    variation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user = build_user_prompt(topic, facts, cfg, variation=variation)
    plan = _call_llm(llm, user, cfg, cache)
    return _plan_to_script(plan, topic, facts, cfg, paths, variation=variation, expansions=0)


def expand_script(
    script: dict[str, Any],
    facts: list[str],
    llm: LLMProvider,
    cfg: Config,
    paths: JobPaths,
    *,
    current_seconds: float,
    add_seconds: float,
    cache: JsonCache | None = None,
) -> dict[str, Any] | None:
    """Ask the writer to add useful context; returns the longer script, or None if it did not grow."""
    topic = str(script.get("topic") or "")
    expand = {
        "existing_script": {
            "title": script["title"],
            "description": script["description"],
            "facts_to_verify": script.get("facts_to_verify", []),
            "lines": script["lines"],
        },
        "current_seconds": round(current_seconds, 1),
        "add_seconds": round(add_seconds, 1),
    }
    user = build_user_prompt(topic, facts, cfg, variation=script.get("variation"), expand=expand)
    plan = _call_llm(llm, user, cfg, cache)
    wps = float(cfg.get("script.words_per_second", 2.6))
    before = estimate_seconds(script["lines"], wps)
    after = estimate_seconds([{"narration": ln.get("narration", "")} for ln in plan.get("lines", [])], wps)
    if after <= before + 0.5:
        log.warning("Expansion did not lengthen the script (%.1fs -> %.1fs); keeping the original", before, after)
        return None
    return _plan_to_script(plan, topic, facts, cfg, paths, variation=script.get("variation"), expansions=int(script.get("expansions", 0)) + 1)
