"""Phase 6: script + shot plan written against the footage that actually exists.

The LLM receives the inventory (and topic.txt if present) and returns narration
lines with scene assignments. The result is schema-validated, then checked
against the editorial rules (banned phrases, unknown scenes, strongest opening,
length, fact-safety flags) and repaired or retried before script.json is written.
"""

from __future__ import annotations

import json
import re
from typing import Any

from autoeditor.cache import JsonCache, make_key
from autoeditor.config import Config
from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.job import JobPaths, write_json
from autoeditor.providers.base import LLMProvider, ProviderError
from autoeditor.schemas import LLM_SHOT_PLAN_SCHEMA, validate

log = get_logger(__name__)

PROMPT_VERSION = "v1"
_WORD = re.compile(r"[A-Za-z0-9']+")
_NUMERIC_CLAIM = re.compile(
    r"(\$\s?\d|\d+(\.\d+)?\s?%|\b(19|20)\d{2}\b|\b\d+(\.\d+)?\s?(mm|cm|inch|inches|grams?|g|kg|lbs?|ounces?|oz|hours?|hrs?|mah|ghz|mhz|gb|tb|mp|megapixels?|nits|fps|million|billion|percent)\b|\b\d{2,}\b)",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """You are the script writer and shot planner for an automated YouTube Shorts editor.
You will receive a FOOTAGE INVENTORY (scenes that exist, with descriptions and scores) and optionally a TOPIC.

Your job: write the narration AND assign existing scenes to every line.

HARD RULES
1. Only reference visuals that exist in the inventory. Every scene_id you use must be in usable_scenes.
2. Prefer visually interesting scenes (higher visual_interest_score / score).
3. Line 1 must use one of strongest_scenes.
4. Do not repeat a scene_id within ~30 seconds of narration unless the footage leaves no choice. Do not let one duplicate_group dominate.
5. A line may use several scene_ids; list them in display order. Aim for a visual change every ~1.5-3 seconds where it helps, but never force cuts on a fixed clock; strong footage may hold longer.
6. Total length must fit the available footage and target the requested duration (35-55 seconds, roughly 2.6 words/second).
7. Spoken style: American English, short sentences, active voice, about 8th-grade readability, strong cold open, escalation, a payoff or twist, and a clean ending that can loop.
8. Never use: "Did you know", "In this video", "mind-blowing", "game changer", "insane", or generic AI hype.
9. Never claim personal experience ("I tested", "I used", "I recommend").
10. FACT SAFETY: footage cannot prove dates, prices, sales figures, specs, historical causes, business decisions, performance statistics or numerical comparisons. If the topic requires such a claim and no verified source is provided, either avoid stating it as fact (use careful, non-committal phrasing) or state it AND add it to facts_to_verify. Never invent facts. Never present unsupported claims as certain.
11. If there is no topic, infer the most coherent story from the footage and stay strictly within what the visuals show.
12. overlay_text is optional: 1-3 words in CAPS for a punchy on-screen label, or null. emphasis_words are words that appear verbatim in that line's narration.

Return only the JSON object."""


def build_user_prompt(inventory: dict[str, Any], topic: str | None, cfg: Config) -> str:
    target_min = int(cfg.get("script.target_min_seconds", 35))
    target_max = int(cfg.get("script.target_max_seconds", 55))
    total = float(inventory.get("total_usable_seconds", 0))
    target = min(target_max, max(target_min, int(total * 0.8))) if total > 0 else target_max
    payload = {
        "topic": topic,
        "target_seconds": target,
        "target_range_seconds": [target_min, target_max],
        "words_per_second": float(cfg.get("script.words_per_second", 2.6)),
        "verified_sources": [],
        "inventory": inventory,
    }
    intro = f"TOPIC: {topic}\n" if topic else "TOPIC: none provided - infer the story from the footage; do not invent facts.\n"
    intro += "No research/source module supplied verified facts for this job (verified_sources is empty).\n"
    return intro + "Write the script and shot plan for this footage. Input JSON:\n" + json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Post-validation of the LLM plan
# --------------------------------------------------------------------------- #
def estimate_seconds(lines: list[dict[str, Any]], wps: float) -> float:
    words = sum(len(_WORD.findall(str(line["narration"]))) for line in lines)
    return round(words / wps, 2) if wps > 0 else 0.0


def find_banned_phrases(lines: list[dict[str, Any]], banned: list[str]) -> list[str]:
    hits: list[str] = []
    for line in lines:
        text = str(line["narration"]).lower()
        for phrase in banned:
            if phrase.lower() in text:
                hits.append(f"line {line['id']}: '{phrase}'")
    return hits


def numeric_claims(lines: list[dict[str, Any]]) -> list[str]:
    """Narration fragments that contain numbers/dates/prices footage cannot prove."""
    found: list[str] = []
    for line in lines:
        text = str(line["narration"])
        if _NUMERIC_CLAIM.search(text):
            found.append(f'Line {line["id"]} contains a numeric/date/price claim that footage cannot verify: "{text.strip()}"')
    return found


def repair_plan(plan: dict[str, Any], inventory: dict[str, Any], cfg: Config) -> tuple[dict[str, Any], list[str]]:
    """Enforce structural rules deterministically; return (plan, notes)."""
    notes: list[str] = []
    usable_ids = {int(s["scene_id"]) for s in inventory.get("usable_scenes", [])}
    strongest = [int(s["scene_id"]) for s in inventory.get("strongest_scenes", [])]
    lines = [dict(line) for line in plan.get("lines", [])]

    # Renumber ids sequentially and drop empty narrations.
    cleaned: list[dict[str, Any]] = []
    for line in lines:
        narration = str(line.get("narration", "")).strip()
        if not narration:
            notes.append(f"dropped empty line {line.get('id')}")
            continue
        ids: list[int] = []
        for sid in line.get("scene_ids", []) or []:
            try:
                sid_int = int(sid)
            except (TypeError, ValueError):
                continue
            if sid_int not in usable_ids:
                notes.append(f"line {line.get('id')}: removed unknown scene {sid}")
                continue
            if ids and ids[-1] == sid_int:
                continue  # consecutive duplicate
            ids.append(sid_int)
        emphasis = [w for w in (line.get("emphasis_words") or []) if isinstance(w, str) and w.strip()]
        narration_lower = narration.lower()
        emphasis = [w for w in emphasis if w.lower().strip(".,!?") in narration_lower]
        overlay = line.get("overlay_text")
        overlay = str(overlay).strip()[:24] if isinstance(overlay, str) and overlay.strip() else None
        cleaned.append({"id": len(cleaned) + 1, "narration": narration, "scene_ids": ids, "overlay_text": overlay, "emphasis_words": emphasis})
    if not cleaned:
        raise ProviderError("shot plan contained no usable lines")

    # Rule 3: first line opens on a strongest scene.
    if strongest and cleaned[0]["scene_ids"][:1] != [strongest[0]] and not set(cleaned[0]["scene_ids"]) & set(strongest):
        cleaned[0]["scene_ids"] = [strongest[0], *cleaned[0]["scene_ids"]]
        notes.append(f"line 1: prepended strongest scene {strongest[0]}")

    # Length: trim trailing lines if far beyond the hard maximum.
    wps = float(cfg.get("script.words_per_second", 2.6))
    hard_max = float(cfg.get("script.hard_max_seconds", 60))
    while len(cleaned) > 3 and estimate_seconds(cleaned, wps) > hard_max:
        removed = cleaned.pop()
        notes.append(f"trimmed line '{removed['narration'][:40]}...' to respect {hard_max}s maximum")
    est = estimate_seconds(cleaned, wps)
    target_min = float(cfg.get("script.target_min_seconds", 35))
    if est < target_min * 0.6:
        notes.append(f"script is short ({est}s estimated); footage or topic may be thin")

    facts = [str(f).strip() for f in plan.get("facts_to_verify", []) if str(f).strip()]
    for claim in numeric_claims(cleaned):
        if claim not in facts:
            facts.append(claim)
            notes.append("auto-flagged numeric claim for verification")

    repaired = {
        "title": str(plan.get("title", "")).strip() or "Untitled",
        "description": str(plan.get("description", "")).strip(),
        "facts_to_verify": facts,
        "lines": cleaned,
    }
    return repaired, notes


def generate_shot_plan(
    inventory: dict[str, Any],
    topic: str | None,
    llm: LLMProvider,
    cfg: Config,
    paths: JobPaths,
    *,
    cache: JsonCache | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Call the LLM, validate, repair and write script.json."""
    if not inventory.get("usable_scenes"):
        raise ProviderError("no usable scenes in inventory; cannot write a script")
    banned = [str(b) for b in cfg.get("script.banned_phrases", [])]
    max_retries = int(cfg.get("llm.max_json_retries", 2))
    max_tokens = int(cfg.get("llm.max_tokens", 16000))
    user = build_user_prompt(inventory, topic, cfg)
    key = make_key("shot_plan", getattr(llm, "name", "llm"), cfg.get("llm.model"), PROMPT_VERSION, user)
    plan: dict[str, Any] | None = None
    if cache is not None and not force:
        cached = cache.get(key)
        if cached:
            log.info("Shot plan served from cache")
            plan = cached
    attempt = 0
    feedback = ""
    while plan is None:
        attempt += 1
        result = llm.complete_json(system=SYSTEM_PROMPT, user=user + feedback, output_schema=LLM_SHOT_PLAN_SCHEMA, max_tokens=max_tokens)
        candidate = result.data
        hits = find_banned_phrases(candidate.get("lines", []), banned)
        if hits and attempt <= max_retries:
            log.warning("Shot plan used banned phrases (%s); retrying", "; ".join(hits))
            feedback = "\n\nREVISION REQUIRED: the previous draft used forbidden phrases: " + "; ".join(hits) + ". Rewrite those lines."
            continue
        if hits:
            raise ProviderError("shot plan still contains banned phrases after retries: " + "; ".join(hits))
        plan = candidate
        if cache is not None:
            cache.put(key, plan)

    repaired, notes = repair_plan(plan, inventory, cfg)
    for note in notes:
        log.info("shot plan: %s", note)
    script = {
        "version": 1,
        "mode": "footage_only",
        "title": repaired["title"],
        "description": repaired["description"],
        "topic": topic,
        "theme": str(cfg.get("render.theme", "default")),
        "facts_to_verify": repaired["facts_to_verify"],
        "lines": repaired["lines"],
    }
    validate(script, "script")
    write_json(paths.script_json, script)
    est = estimate_seconds(script["lines"], float(cfg.get("script.words_per_second", 2.6)))
    log.info("Script: %d lines, ~%.0fs estimated, %d fact(s) to verify", len(script["lines"]), est, len(script["facts_to_verify"]))
    return script
