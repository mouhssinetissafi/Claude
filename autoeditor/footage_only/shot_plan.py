"""Phase 6: script + shot plan written against the footage that actually exists.

The LLM receives the inventory (and topic.txt if present) and returns narration
lines with scene assignments. The result is schema-validated, then checked
against the editorial rules (banned phrases, unknown scenes, strongest opening,
length, fact-safety flags) and repaired or retried before script.json is written.

Every job carries a *variation profile* (hook, structure, ending, pacing,
opener rotation) so consecutive Shorts do not share the same pattern, and a
script that lands short of the minimum duration can be *expanded* with useful
context rather than filler.
"""

from __future__ import annotations

import json
import re
from typing import Any

from autoeditor.cache import JsonCache, make_key
from autoeditor.config import Config
from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.duration import expansion_instruction, policy_from_config
from autoeditor.pipeline.job import JobPaths, write_json
from autoeditor.pipeline.originality import variation_instruction
from autoeditor.providers.base import LLMProvider, ProviderError
from autoeditor.schemas import LLM_SHOT_PLAN_SCHEMA, validate

log = get_logger(__name__)

PROMPT_VERSION = "v3"
_WORD = re.compile(r"[A-Za-z0-9']+")
_NUMERIC_CLAIM = re.compile(
    r"(\$\s?\d|\d+(\.\d+)?\s?%|\b(19|20)\d{2}\b|\b\d+(\.\d+)?\s?(mm|cm|inch|inches|grams?|g|kg|lbs?|ounces?|oz|hours?|hrs?|mah|ghz|mhz|gb|tb|mp|megapixels?|nits|fps|million|billion|percent)\b|\b\d{2,}\b)",
    re.IGNORECASE,
)



MANUAL_ASSIGNMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["assignments"],
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["line_id", "scene_ids", "overlay_text", "emphasis_words"],
                "properties": {
                    "line_id": {"type": "integer", "minimum": 1},
                    "scene_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}},
                    "overlay_text": {"type": ["string", "null"]},
                    "emphasis_words": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}

MANUAL_ASSIGNMENT_SYSTEM = """You are the shot-matching editor for a vertical short.
The narration is USER-SUPPLIED and locked: never rewrite, summarize or add words.
For each narration line, select only scene IDs that exist in usable_scenes and visually support that exact line.
Prefer strong, relevant scenes, vary the visuals, avoid rapid unnecessary repeats, and use a strongest scene for line 1.
You may add a short overlay_text and emphasis_words only when they come directly from the supplied line.
Return only the requested JSON assignments."""


def split_manual_script(text: str) -> list[str]:
    """Split a user-supplied script into voice/editing lines without rewriting it."""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        raise ProviderError("manual script is empty")
    explicit = [line.strip() for line in cleaned.split("\n") if line.strip()]
    if len(explicit) > 1:
        return explicit
    # A pasted paragraph is split only at sentence boundaries.  The words and
    # punctuation remain exactly as supplied; this is segmentation, not writing.
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    return parts or [cleaned]


def _deterministic_manual_assignments(lines: list[str], inventory: dict[str, Any], opener_hint: int | None) -> list[dict[str, Any]]:
    usable = list(inventory.get("usable_scenes", []))
    strongest = [int(s["scene_id"]) for s in inventory.get("strongest_scenes", [])]
    ranked = sorted(usable, key=lambda s: (-float(s.get("visual_interest_score", 0)), int(s["scene_id"])))
    order = [int(s["scene_id"]) for s in ranked]
    if opener_hint in order:
        order.remove(int(opener_hint))
        order.insert(0, int(opener_hint))
    elif strongest:
        first = strongest[0]
        if first in order:
            order.remove(first)
        order.insert(0, first)
    if not order:
        raise ProviderError("no usable scenes in inventory; cannot match a manual script")
    out: list[dict[str, Any]] = []
    cursor = 0
    for line_id, narration in enumerate(lines, start=1):
        words = _WORD.findall(narration)
        desired = 1 if len(words) < 10 else 2 if len(words) < 22 else 3
        scene_ids = [order[(cursor + offset) % len(order)] for offset in range(desired)]
        cursor += desired
        emphasis = [word for word in words if len(word) >= 7][:1]
        out.append({
            "line_id": line_id,
            "scene_ids": scene_ids,
            "overlay_text": emphasis[0].upper()[:24] if emphasis and line_id % 3 == 1 else None,
            "emphasis_words": emphasis,
        })
    return out


def plan_manual_script(
    manual_text: str,
    inventory: dict[str, Any],
    topic: str | None,
    llm: LLMProvider,
    cfg: Config,
    paths: JobPaths,
    *,
    cache: JsonCache | None = None,
    force: bool = False,
    opener_hint: int | None = None,
) -> dict[str, Any]:
    """Preserve a user's script verbatim while assigning available visuals to it."""
    lines = split_manual_script(manual_text)
    usable_ids = {int(s["scene_id"]) for s in inventory.get("usable_scenes", [])}
    if not usable_ids:
        raise ProviderError("no usable scenes in inventory; cannot match a manual script")

    if getattr(llm, "name", "") == "mock":
        assignments = _deterministic_manual_assignments(lines, inventory, opener_hint)
    else:
        payload = {
            "topic": topic,
            "locked_narration": [{"line_id": i + 1, "narration": line} for i, line in enumerate(lines)],
            "preferred_opener_scene_id": opener_hint,
            "inventory": inventory,
        }
        user = "Match existing footage to this locked user script. Input JSON:\n" + json.dumps(payload, ensure_ascii=False)
        key = make_key("manual_scene_assignment", getattr(llm, "name", "llm"), cfg.get("llm.model"), PROMPT_VERSION, user)
        result_data = None if force or cache is None else cache.get(key)
        if result_data is None:
            result = llm.complete_json(
                system=MANUAL_ASSIGNMENT_SYSTEM,
                user=user,
                output_schema=MANUAL_ASSIGNMENT_SCHEMA,
                max_tokens=min(8000, int(cfg.get("llm.max_tokens", 16000))),
            )
            result_data = result.data
            if cache is not None:
                cache.put(key, result_data)
        assignments = list(result_data.get("assignments", []))

    by_id = {int(a.get("line_id", 0)): a for a in assignments}
    plan_lines: list[dict[str, Any]] = []
    for line_id, narration in enumerate(lines, start=1):
        assignment = by_id.get(line_id, {})
        scene_ids: list[int] = []
        for raw in assignment.get("scene_ids", []) or []:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                continue
            if sid in usable_ids and sid not in scene_ids:
                scene_ids.append(sid)
        if not scene_ids:
            fallback = _deterministic_manual_assignments([narration], inventory, opener_hint if line_id == 1 else None)[0]
            scene_ids = list(fallback["scene_ids"])
        overlay = assignment.get("overlay_text")
        if isinstance(overlay, str) and overlay.strip():
            overlay = overlay.strip()[:24]
        else:
            overlay = None
        emphasis = [str(w).strip() for w in assignment.get("emphasis_words", []) or [] if str(w).strip() and str(w).lower().strip(".,!?") in narration.lower()]
        plan_lines.append({"id": line_id, "narration": narration, "scene_ids": scene_ids, "overlay_text": overlay, "emphasis_words": emphasis})

    plan = {
        "title": (topic or "Manual script").strip()[:100],
        "description": "User-supplied narration with visuals matched from the project media.",
        "facts_to_verify": numeric_claims(plan_lines),
        "lines": plan_lines,
    }
    repaired, notes = repair_plan(plan, inventory, cfg, opener_hint=opener_hint)
    # repair_plan must never rewrite narration. Guard that invariant explicitly.
    if [ln["narration"] for ln in repaired["lines"]] != lines[: len(repaired["lines"])]:
        raise ProviderError("manual-script safety check failed: narration changed during planning")
    for note in notes:
        log.info("manual shot plan: %s", note)
    return _finish_script(repaired, topic, cfg, paths, variation=None, expansions=0)

SYSTEM_PROMPT = """You are the script writer and shot planner for an automated YouTube Shorts editor.
You will receive a FOOTAGE INVENTORY (scenes that exist, with descriptions and scores) and optionally a TOPIC.

Your job: write the narration AND assign existing scenes to every line.

HARD RULES
1. Only reference visuals that exist in the inventory. Every scene_id you use must be in usable_scenes.
2. Prefer visually interesting scenes (higher visual_interest_score / score).
3. Line 1 must use one of strongest_scenes (the input names a preferred opener).
4. Do not repeat a scene_id within ~30 seconds of narration unless the footage leaves no choice. Do not let one duplicate_group dominate. Use the footage meaningfully: each scene should illustrate the line it is attached to.
5. A line may use several scene_ids; list them in display order. Aim for a visual change every ~1.5-3 seconds where it helps, but never force cuts on a fixed clock; strong footage may hold longer.
6. Total length must fit the available footage and land inside target_range_seconds (at roughly words_per_second). Never go below min_seconds. Reach the length with useful, specific content, never with filler or repetition.
7. Spoken style: American English, short sentences, active voice, about 8th-grade readability, strong cold open, escalation, a payoff or twist, and a clean ending that can loop.
8. Never use: "Did you know", "In this video", "mind-blowing", "game changer", "insane", or generic AI hype.
9. Never claim personal experience ("I tested", "I used", "I recommend").
10. FACT SAFETY: footage cannot prove dates, prices, sales figures, specs, historical causes, business decisions, performance statistics or numerical comparisons. If the topic requires such a claim and no verified source is provided, either avoid stating it as fact (use careful, non-committal phrasing) or state it AND add it to facts_to_verify. Never invent facts. Never present unsupported claims as certain.
11. If there is no topic, infer the most coherent story from the footage and stay strictly within what the visuals show.
12. overlay_text is optional: 1-3 words in CAPS for a punchy on-screen label, or null. emphasis_words are words that appear verbatim in that line's narration.
13. ORIGINALITY: this must be an original piece of writing. Do not reproduce, closely paraphrase or lightly reword any article, press release, product page, review or another creator's script. Follow the VARIATION PROFILE so the hook, structure and ending differ from other videos.
14. PHOTOS: scenes with kind "image" are still photographs; the editor animates them with a slow camera move named by "framing" (pan, push, detail, reveal). Each framing holds about 2-4 seconds, so give a photo line 1-2 framings, not more. Write to what the picture shows and never describe motion, sound or a sequence of events a single photo cannot contain. Spread framings of the same photo across the video instead of stacking them.

Return only the JSON object."""


def build_user_prompt(
    inventory: dict[str, Any],
    topic: str | None,
    cfg: Config,
    *,
    variation: dict[str, Any] | None = None,
    opener_hint: int | None = None,
    expand: dict[str, Any] | None = None,
    house_style: str | None = None,
) -> str:
    policy = policy_from_config(cfg)
    total = float(inventory.get("total_usable_seconds", 0))
    target = min(policy.target_max_seconds, max(policy.target_min_seconds, int(total * 0.8))) if total > 0 else policy.target_max_seconds
    payload: dict[str, Any] = {
        "topic": topic,
        "target_seconds": target,
        "target_range_seconds": [policy.target_min_seconds, policy.target_max_seconds],
        "min_seconds": policy.min_final_seconds,
        "hard_max_seconds": policy.hard_max_seconds,
        "words_per_second": policy.words_per_second,
        "verified_sources": [],
        "preferred_opener_scene_id": opener_hint,
        "variation": variation,
        "inventory": inventory,
    }
    if expand:
        payload["expand"] = expand
    intro = f"TOPIC: {topic}\n" if topic else "TOPIC: none provided - infer the story from the footage; do not invent facts.\n"
    intro += "No research/source module supplied verified facts for this job (verified_sources is empty).\n"
    if house_style:
        intro += house_style + "\n"
    if variation:
        intro += variation_instruction(variation) + "\n"
    if expand:
        intro += expansion_instruction(float(expand["current_seconds"]), float(expand["add_seconds"]), policy) + "\n"
        return intro + "Revise and expand the existing script and shot plan. Input JSON:\n" + json.dumps(payload, ensure_ascii=False)
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


def repair_plan(plan: dict[str, Any], inventory: dict[str, Any], cfg: Config, *, opener_hint: int | None = None) -> tuple[dict[str, Any], list[str]]:
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

    # Rule 3: first line opens on a strongest scene (the rotated opener hint when given).
    if strongest and cleaned[0]["scene_ids"][:1] != [strongest[0]] and not set(cleaned[0]["scene_ids"]) & set(strongest):
        opener = opener_hint if opener_hint in strongest else strongest[0]
        cleaned[0]["scene_ids"] = [opener, *cleaned[0]["scene_ids"]]
        notes.append(f"line 1: prepended strongest scene {opener}")

    # Length: trim trailing lines if far beyond the hard maximum.
    policy = policy_from_config(cfg)
    while len(cleaned) > 3 and estimate_seconds(cleaned, policy.words_per_second) > policy.hard_max_seconds:
        removed = cleaned.pop()
        notes.append(f"trimmed line '{removed['narration'][:40]}...' to respect {policy.hard_max_seconds:.0f}s maximum")
    est = estimate_seconds(cleaned, policy.words_per_second)
    if est < policy.min_final_seconds:
        notes.append(f"script is short ({est}s estimated, minimum {policy.min_final_seconds:.0f}s); expansion will be requested")

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


def _call_llm(llm: LLMProvider, user: str, cfg: Config, cache: JsonCache | None, *, force: bool) -> dict[str, Any]:
    banned = [str(b) for b in cfg.get("script.banned_phrases", [])]
    max_retries = int(cfg.get("llm.max_json_retries", 2))
    max_tokens = int(cfg.get("llm.max_tokens", 16000))
    key = make_key("shot_plan", getattr(llm, "name", "llm"), cfg.get("llm.model"), PROMPT_VERSION, user)
    if cache is not None and not force:
        cached = cache.get(key)
        if cached:
            log.info("Shot plan served from cache")
            return dict(cached)
    attempt = 0
    feedback = ""
    while True:
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
        if cache is not None:
            cache.put(key, candidate)
        return candidate


def _finish_script(
    repaired: dict[str, Any],
    topic: str | None,
    cfg: Config,
    paths: JobPaths,
    *,
    variation: dict[str, Any] | None,
    expansions: int,
) -> dict[str, Any]:
    script = {
        "version": 1,
        "mode": "footage_only",
        "title": repaired["title"],
        "description": repaired["description"],
        "topic": topic,
        "theme": str(cfg.get("render.theme", "default")),
        "facts_to_verify": repaired["facts_to_verify"],
        "variation": variation,
        "expansions": expansions,
        "lines": repaired["lines"],
    }
    validate(script, "script")
    write_json(paths.script_json, script)
    est = estimate_seconds(script["lines"], float(cfg.get("script.words_per_second", 2.6)))
    log.info("Script: %d lines, ~%.0fs estimated, %d fact(s) to verify, %d expansion(s)", len(script["lines"]), est, len(script["facts_to_verify"]), expansions)
    return script


def generate_shot_plan(
    inventory: dict[str, Any],
    topic: str | None,
    llm: LLMProvider,
    cfg: Config,
    paths: JobPaths,
    *,
    cache: JsonCache | None = None,
    force: bool = False,
    variation: dict[str, Any] | None = None,
    opener_hint: int | None = None,
    house_style: str | None = None,
) -> dict[str, Any]:
    """Call the LLM, validate, repair and write script.json."""
    if not inventory.get("usable_scenes"):
        raise ProviderError("no usable scenes in inventory; cannot write a script")
    user = build_user_prompt(inventory, topic, cfg, variation=variation, opener_hint=opener_hint, house_style=house_style)
    plan = _call_llm(llm, user, cfg, cache, force=force)
    repaired, notes = repair_plan(plan, inventory, cfg, opener_hint=opener_hint)
    for note in notes:
        log.info("shot plan: %s", note)
    return _finish_script(repaired, topic, cfg, paths, variation=variation, expansions=0)


def expand_shot_plan(
    script: dict[str, Any],
    inventory: dict[str, Any],
    llm: LLMProvider,
    cfg: Config,
    paths: JobPaths,
    *,
    current_seconds: float,
    add_seconds: float,
    cache: JsonCache | None = None,
    house_style: str | None = None,
) -> dict[str, Any] | None:
    """Ask the writer to add useful context; returns the longer script, or None if it did not grow."""
    variation = script.get("variation")
    opener_hint = script["lines"][0]["scene_ids"][0] if script["lines"] and script["lines"][0].get("scene_ids") else None
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
    user = build_user_prompt(inventory, script.get("topic"), cfg, variation=variation, opener_hint=opener_hint, expand=expand, house_style=house_style)
    plan = _call_llm(llm, user, cfg, cache, force=False)
    repaired, notes = repair_plan(plan, inventory, cfg, opener_hint=opener_hint)
    for note in notes:
        log.info("expansion: %s", note)
    wps = float(cfg.get("script.words_per_second", 2.6))
    before = estimate_seconds(script["lines"], wps)
    after = estimate_seconds(repaired["lines"], wps)
    if after <= before + 0.5:
        log.warning("Expansion did not lengthen the script (%.1fs -> %.1fs); keeping the original", before, after)
        return None
    log.info("Expansion: %.1fs -> %.1fs estimated", before, after)
    return _finish_script(repaired, script.get("topic"), cfg, paths, variation=variation, expansions=int(script.get("expansions", 0)) + 1)
