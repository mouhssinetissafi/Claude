"""JSON schemas for every artifact that crosses a module boundary.

Everything the LLM returns is validated with these schemas before use. The same
schemas describe the files Remotion consumes (script.json, captions.json,
timeline.json), so the Python and TypeScript sides share one contract. The
TypeScript mirror lives in ``remotion/src/types.ts``.
"""

from __future__ import annotations

import json
import re
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator


class SchemaValidationError(ValueError):
    """Raised when a document does not match its schema."""

    def __init__(self, schema_name: str, errors: list[str]) -> None:
        self.schema_name = schema_name
        self.errors = errors
        super().__init__(f"{schema_name} failed validation: " + "; ".join(errors[:6]))


class LLMJsonError(ValueError):
    """Raised when text from the model cannot be turned into a JSON object."""


# --------------------------------------------------------------------------- #
# Scene analysis (Phase 4)
# --------------------------------------------------------------------------- #
SCENE_ANALYSIS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "scene_id",
        "description",
        "subjects",
        "objects",
        "environment",
        "shot_type",
        "camera_motion",
        "subject_motion",
        "mood",
        "colors",
        "quality_score",
        "visual_interest_score",
        "watermark_detected",
        "text_detected",
        "text_content",
        "product_or_brand",
        "possible_topics",
        "safe_to_use",
    ],
    "properties": {
        "scene_id": {"type": "integer", "minimum": 1},
        "description": {"type": "string"},
        "subjects": {"type": "array", "items": {"type": "string"}},
        "objects": {"type": "array", "items": {"type": "string"}},
        "environment": {"type": "string"},
        "shot_type": {"type": "string"},
        "camera_motion": {"type": "string"},
        "subject_motion": {"type": "string"},
        "mood": {"type": "string"},
        "colors": {"type": "array", "items": {"type": "string"}},
        "quality_score": {"type": "number", "minimum": 0, "maximum": 100},
        "visual_interest_score": {"type": "number", "minimum": 0, "maximum": 100},
        "watermark_detected": {"type": "boolean"},
        "text_detected": {"type": "boolean"},
        "text_content": {"type": "string"},
        "product_or_brand": {"type": "array", "items": {"type": "string"}},
        "possible_topics": {"type": "array", "items": {"type": "string"}},
        "safe_to_use": {"type": "boolean"},
    },
}

# Camera path over a still photograph (footage-only photo framings). Focal points are
# fractions of the source image; scale is relative to the cover fit of the frame.
MOTION_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["scale", "x", "y"],
    "properties": {
        "scale": {"type": "number", "minimum": 1.0},
        "x": {"type": "number", "minimum": 0, "maximum": 1},
        "y": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

SEGMENT_MOTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["framing", "src_width", "src_height", "from", "to"],
    "properties": {
        "framing": {"type": "string", "enum": ["pan", "push", "detail", "reveal"]},
        "primary": {"type": "boolean"},
        "fit": {"type": "string", "enum": ["cover"]},
        "src_width": {"type": "integer", "minimum": 1},
        "src_height": {"type": "integer", "minimum": 1},
        "hold_seconds": {"type": "number", "minimum": 0},
        "ease": {"type": "string", "enum": ["inout", "linear"]},
        "from": MOTION_STATE_SCHEMA,
        "to": MOTION_STATE_SCHEMA,
    },
}

SCENE_RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["scene_id", "source_file", "start_time", "end_time", "duration"],
    "properties": {
        "scene_id": {"type": "integer", "minimum": 1},
        "source_file": {"type": "string"},
        "normalized_file": {"type": "string"},
        "start_time": {"type": "number", "minimum": 0},
        "end_time": {"type": "number", "minimum": 0},
        "duration": {"type": "number", "minimum": 0},
        "frames": {"type": "array", "items": {"type": "string"}},
        "content_hash": {"type": "string"},
        "analysis": {"anyOf": [{"type": "null"}, SCENE_ANALYSIS_SCHEMA]},
        "rejected_reason": {"type": ["string", "null"]},
        "kind": {"type": "string", "enum": ["video", "image"]},
        "motion": {"anyOf": [{"type": "null"}, SEGMENT_MOTION_SCHEMA]},
    },
}

SCENES_FILE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["version", "job", "scenes"],
    "properties": {
        "version": {"type": "integer"},
        "job": {"type": "string"},
        "sources": {"type": "array"},
        "scenes": {"type": "array", "items": SCENE_RECORD_SCHEMA},
    },
}

# --------------------------------------------------------------------------- #
# Script (Phase 6 and normal mode)
# --------------------------------------------------------------------------- #
SCRIPT_LINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "narration"],
    "properties": {
        "id": {"type": "integer", "minimum": 1},
        "narration": {"type": "string", "minLength": 1},
        "scene_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "overlay_text": {"type": ["string", "null"]},
        "emphasis_words": {"type": "array", "items": {"type": "string"}},
        "media_query": {"type": ["string", "null"]},
        "media_path": {"type": ["string", "null"]},
    },
}

SCRIPT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["title", "description", "lines"],
    "properties": {
        "version": {"type": "integer"},
        "mode": {"type": "string", "enum": ["normal", "footage_only"]},
        "title": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "topic": {"type": ["string", "null"]},
        "theme": {"type": "string"},
        "facts_to_verify": {"type": "array", "items": {"type": "string"}},
        "variation": {"type": ["object", "null"]},
        "expansions": {"type": "integer", "minimum": 0},
        "lines": {"type": "array", "minItems": 1, "items": SCRIPT_LINE_SCHEMA},
    },
}

# Strict variant handed to the model as output_config.format (no extra keys).
LLM_SHOT_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "description", "facts_to_verify", "lines"],
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "facts_to_verify": {"type": "array", "items": {"type": "string"}},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "narration", "scene_ids", "overlay_text", "emphasis_words"],
                "properties": {
                    "id": {"type": "integer"},
                    "narration": {"type": "string"},
                    "scene_ids": {"type": "array", "items": {"type": "integer"}},
                    "overlay_text": {"type": ["string", "null"]},
                    "emphasis_words": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

LLM_NORMAL_SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "description", "facts_to_verify", "lines"],
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "facts_to_verify": {"type": "array", "items": {"type": "string"}},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "narration", "media_query", "overlay_text", "emphasis_words"],
                "properties": {
                    "id": {"type": "integer"},
                    "narration": {"type": "string"},
                    "media_query": {"type": "string"},
                    "overlay_text": {"type": ["string", "null"]},
                    "emphasis_words": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

# --------------------------------------------------------------------------- #
# Voice timing (Phase 7)
# --------------------------------------------------------------------------- #
VOICE_TIMING_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "array",
    "items": {
        "type": "object",
        "required": ["line_id", "start", "end", "duration"],
        "properties": {
            "line_id": {"type": "integer"},
            "start": {"type": "number", "minimum": 0},
            "end": {"type": "number", "minimum": 0},
            "duration": {"type": "number", "minimum": 0},
            "file": {"type": "string"},
        },
    },
}

# --------------------------------------------------------------------------- #
# Captions (Phase 8)
# --------------------------------------------------------------------------- #
CAPTION_WORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["text", "start", "end"],
    "properties": {
        "text": {"type": "string"},
        "start": {"type": "number", "minimum": 0},
        "end": {"type": "number", "minimum": 0},
        "line_id": {"type": ["integer", "null"]},
        "emphasis": {"type": "boolean"},
    },
}

CAPTIONS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["version", "language", "duration", "words", "segments"],
    "properties": {
        "version": {"type": "integer"},
        "language": {"type": "string"},
        "duration": {"type": "number", "minimum": 0},
        "words": {"type": "array", "items": CAPTION_WORD_SCHEMA},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "start", "end", "words"],
                "properties": {
                    "text": {"type": "string"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "words": {"type": "array", "items": CAPTION_WORD_SCHEMA},
                },
            },
        },
    },
}

# --------------------------------------------------------------------------- #
# Timeline (Phase 9)
# --------------------------------------------------------------------------- #
TIMELINE_SEGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["src", "type", "start", "end"],
    "properties": {
        "src": {"type": "string"},
        "type": {"type": "string", "enum": ["video", "image"]},
        "start": {"type": "number", "minimum": 0},
        "end": {"type": "number", "minimum": 0},
        "source_start": {"type": "number", "minimum": 0},
        "source_end": {"type": "number", "minimum": 0},
        "scene_id": {"type": ["integer", "null"]},
        "effect": {"type": "string", "enum": ["none", "kenburns", "zoom_in", "zoom_out"]},
        "transition": {"type": "string", "enum": ["cut", "fade"]},
        "fallback": {"type": ["string", "null"]},
        "motion": {"anyOf": [{"type": "null"}, SEGMENT_MOTION_SCHEMA]},
    },
}

TIMELINE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["version", "fps", "width", "height", "duration", "voice", "lines"],
    "properties": {
        "version": {"type": "integer"},
        "fps": {"type": "integer", "minimum": 1},
        "width": {"type": "integer", "minimum": 1},
        "height": {"type": "integer", "minimum": 1},
        "duration": {"type": "number", "minimum": 0},
        "voice": {"type": "string"},
        "music": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "required": ["src", "volume"],
                    "properties": {
                        "src": {"type": "string"},
                        "volume": {"type": "number"},
                        "ducking": {"type": "boolean"},
                        "ducking_volume": {"type": "number"},
                    },
                },
            ]
        },
        "sfx": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["src", "at"],
                "properties": {
                    "src": {"type": "string"},
                    "at": {"type": "number"},
                    "volume": {"type": "number"},
                },
            },
        },
        "watermark": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "required": ["src", "position"],
                    "properties": {
                        "src": {"type": "string"},
                        "position": {"type": "string", "enum": ["top-right", "top-left", "bottom-right", "bottom-left"]},
                        "width_fraction": {"type": "number", "minimum": 0, "maximum": 1},
                        "max_height_fraction": {"type": "number", "minimum": 0, "maximum": 1},
                        "opacity": {"type": "number", "minimum": 0, "maximum": 1},
                        "margin": {"type": "integer", "minimum": 0},
                    },
                },
            ]
        },
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["line_id", "start", "end", "segments"],
                "properties": {
                    "line_id": {"type": "integer"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "overlay_text": {"type": ["string", "null"]},
                    "emphasis_words": {"type": "array", "items": {"type": "string"}},
                    "segments": {"type": "array", "items": TIMELINE_SEGMENT_SCHEMA},
                },
            },
        },
    },
}

# --------------------------------------------------------------------------- #
# QC and metadata (Phases 11 and 12)
# --------------------------------------------------------------------------- #
QC_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["passed", "checks", "warnings"],
    "properties": {
        "passed": {"type": "boolean"},
        "checks": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["passed"],
                "properties": {
                    "passed": {"type": "boolean"},
                    "value": {},
                    "expected": {},
                    "message": {"type": "string"},
                },
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
        "output": {"type": ["string", "null"]},
    },
}

METADATA_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["title", "description", "hashtags", "tags"],
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "facts_to_verify": {"type": "array", "items": {"type": "string"}},
        "thumbnail_base": {"type": ["string", "null"]},
        "ai_disclosure": {"type": ["object", "null"]},
        "originality": {"type": ["object", "null"]},
        "review_required": {"type": "boolean"},
    },
}

LLM_METADATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "description", "hashtags", "tags"],
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "scene_analysis": SCENE_ANALYSIS_SCHEMA,
    "scenes": SCENES_FILE_SCHEMA,
    "script": SCRIPT_SCHEMA,
    "llm_shot_plan": LLM_SHOT_PLAN_SCHEMA,
    "llm_normal_script": LLM_NORMAL_SCRIPT_SCHEMA,
    "voice_timing": VOICE_TIMING_SCHEMA,
    "captions": CAPTIONS_SCHEMA,
    "timeline": TIMELINE_SCHEMA,
    "qc": QC_SCHEMA,
    "metadata": METADATA_SCHEMA,
    "llm_metadata": LLM_METADATA_SCHEMA,
}


def validate(document: Any, schema_name: str) -> None:
    """Validate ``document`` against a named schema; raise SchemaValidationError."""
    schema = SCHEMAS[schema_name]
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    if errors:
        rendered = []
        for err in errors:
            location = "/".join(str(p) for p in err.path) or "<root>"
            rendered.append(f"{location}: {err.message}")
        raise SchemaValidationError(schema_name, rendered)


def is_valid(document: Any, schema_name: str) -> bool:
    try:
        validate(document, schema_name)
    except (SchemaValidationError, jsonschema.SchemaError):
        return False
    return True


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from model text.

    Handles plain JSON, fenced ```json blocks, and prose around the object.
    Raises LLMJsonError if nothing parses to an object.
    """
    if text is None:
        raise LLMJsonError("empty response")
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    for m in _FENCE_RE.finditer(text):
        candidates.append(m.group(1).strip())
    # Brace-balanced scan for the outermost object.
    start = text.find("{")
    if start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break
    last_error: Exception | None = None
    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(parsed, dict):
            return parsed
        last_error = LLMJsonError("top-level JSON is not an object")
    raise LLMJsonError(f"could not parse JSON object from model output: {last_error}")
