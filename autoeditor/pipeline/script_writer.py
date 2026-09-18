"""Normal mode script writer: topic (+ optional facts) -> script.json.

Lines carry a ``media_query`` that the media stage resolves against the local
library or a stock provider. The same editorial and fact-safety rules as the
footage-only shot planner apply.
"""

from __future__ import annotations

import json
import re
from typing import Any

from autoeditor.cache import JsonCache, make_key
from autoeditor.config import Config
from autoeditor.footage_only.shot_plan import estimate_seconds, find_banned_phrases, numeric_claims
from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.job import JobPaths, write_json
from autoeditor.providers.base import LLMProvider, ProviderError
from autoeditor.schemas import LLM_NORMAL_SCRIPT_SCHEMA, validate

log = get_logger(__name__)

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """You write narration for automated YouTube Shorts.
Rules: American English, short sentences, active voice, ~8th-grade readability, strong cold open,
escalation, a payoff, and a clean loopable ending. Target 35-55 seconds at ~2.6 words/second.
Never use "Did you know", "In this video", "mind-blowing", "game changer", "insane" or generic hype.
Never claim personal experience ("I tested", "I used", "I recommend").
Only state facts that appear in the provided facts list. Any date, price, statistic, spec or causal
claim that is not in the facts list must be avoided or added to facts_to_verify - never invented.
Each line needs a short media_query (2-5 words) describing stock footage that would illustrate it.
overlay_text is 1-3 CAPS words or null; emphasis_words must appear verbatim in the narration.
Return only the JSON object."""


def build_user_prompt(topic: str, facts: list[str], cfg: Config) -> str:
    payload = {
        "topic": topic,
        "facts": facts,
        "target_range_seconds": [cfg.get("script.target_min_seconds", 35), cfg.get("script.target_max_seconds", 55)],
        "words_per_second": cfg.get("script.words_per_second", 2.6),
    }
    return "Write the script. Input JSON:\n" + json.dumps(payload, ensure_ascii=False)


def read_facts(text: str | None) -> list[str]:
    if not text:
        return []
    return [re.sub(r"^[-*\d.)\s]+", "", ln).strip() for ln in text.splitlines() if ln.strip()]


def write_script(topic: str, facts: list[str], llm: LLMProvider, cfg: Config, paths: JobPaths, *, cache: JsonCache | None = None) -> dict[str, Any]:
    banned = [str(b) for b in cfg.get("script.banned_phrases", [])]
    user = build_user_prompt(topic, facts, cfg)
    key = make_key("normal_script", getattr(llm, "name", "llm"), cfg.get("llm.model"), PROMPT_VERSION, user)
    plan = cache.get(key) if cache else None
    feedback = ""
    attempt = 0
    while plan is None:
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
        plan = result.data
        if cache:
            cache.put(key, plan)

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
        "lines": lines,
    }
    validate(script, "script")
    write_json(paths.script_json, script)
    log.info("Script: %d lines, ~%.0fs estimated", len(lines), estimate_seconds(lines, float(cfg.get("script.words_per_second", 2.6))))
    return script
