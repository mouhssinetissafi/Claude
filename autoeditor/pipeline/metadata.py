"""Phase 12: title/description/hashtags/tags and thumbnail base frame."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from autoeditor.config import Config
from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.job import JobPaths, write_json
from autoeditor.providers.base import LLMProvider, ProviderError
from autoeditor.schemas import LLM_METADATA_SCHEMA, validate

log = get_logger(__name__)

SYSTEM_PROMPT = (
    "You write honest YouTube Shorts metadata. No clickbait, no unverifiable claims, no exclamation "
    "spam, no fake urgency. Titles are specific and calm. Hashtags start with '#'. Tags are lowercase "
    "keywords without '#'. Return JSON only."
)

_WORD = re.compile(r"[A-Za-z0-9]+")


def _fallback_metadata(script: dict[str, Any], cfg: Config) -> dict[str, Any]:
    words = [w.lower() for w in _WORD.findall(f"{script.get('topic') or ''} {script['title']}") if len(w) > 3]
    uniq: list[str] = []
    for w in words:
        if w not in uniq:
            uniq.append(w)
    return {
        "title": script["title"],
        "description": script["description"],
        "hashtags": list(cfg.get("metadata.default_hashtags", ["#shorts"])) + [f"#{w}" for w in uniq[:3]],
        "tags": uniq[:10],
    }


def build_metadata(
    script: dict[str, Any],
    paths: JobPaths,
    llm: LLMProvider | None,
    cfg: Config,
    *,
    thumbnail_source: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "metadata_request": True,
        "title": script["title"],
        "description": script["description"],
        "topic": script.get("topic"),
        "narration": " ".join(line["narration"] for line in script["lines"]),
        "facts_to_verify": script.get("facts_to_verify", []),
        "default_hashtags": cfg.get("metadata.default_hashtags", ["#shorts"]),
    }
    data: dict[str, Any]
    if llm is None:
        data = _fallback_metadata(script, cfg)
    else:
        try:
            result = llm.complete_json(
                system=SYSTEM_PROMPT,
                user="Write metadata for this short. Keep the title under 90 characters. Input:\n" + json.dumps(payload, ensure_ascii=False),
                output_schema=LLM_METADATA_SCHEMA,
                max_tokens=2000,
            )
            data = result.data
        except ProviderError as exc:
            log.warning("Metadata LLM call failed (%s); using script title/description", exc)
            data = _fallback_metadata(script, cfg)

    max_title = int(cfg.get("metadata.max_title_chars", 100))
    max_desc = int(cfg.get("metadata.max_description_chars", 4500))
    hashtags = [h if h.startswith("#") else f"#{h}" for h in data.get("hashtags", []) if h.strip()]
    for default in cfg.get("metadata.default_hashtags", ["#shorts"]):
        if default.lower() not in {h.lower() for h in hashtags}:
            hashtags.append(default)
    description = str(data["description"]).strip()
    facts = list(script.get("facts_to_verify", []))
    if facts:
        description += "\n\n[REVIEW BEFORE PUBLISHING] Unverified claims flagged by the script writer:\n" + "\n".join(f"- {f}" for f in facts)
    metadata = {
        "title": str(data["title"]).strip()[:max_title],
        "description": (description + "\n\n" + " ".join(hashtags)).strip()[:max_desc],
        "hashtags": hashtags,
        "tags": [str(t).strip().lower().lstrip("#") for t in data.get("tags", []) if str(t).strip()][:30],
        "facts_to_verify": facts,
        "thumbnail_base": None,
        "review_required": True,
    }
    for key, value in (extra or {}).items():
        if key in {"ai_disclosure", "originality", "review_required"}:
            metadata[key] = value
    if thumbnail_source and thumbnail_source.exists():
        shutil.copy2(thumbnail_source, paths.thumbnail_jpg)
        metadata["thumbnail_base"] = paths.thumbnail_jpg.name
    validate(metadata, "metadata")
    write_json(paths.metadata_json, metadata)
    return metadata
