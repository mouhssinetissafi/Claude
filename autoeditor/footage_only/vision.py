"""Phase 4: vision analysis of scene frames with content-hash caching."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autoeditor.cache import JsonCache, hash_file, make_key
from autoeditor.config import Config
from autoeditor.footage_only.photos import FRAMING_LABELS
from autoeditor.footage_only.scenes import Scene
from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.job import JobPaths, write_json
from autoeditor.providers.base import ProviderError, VisionProvider
from autoeditor.schemas import SCENE_ANALYSIS_SCHEMA, SchemaValidationError, validate

log = get_logger(__name__)

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """You analyze still frames from stock/video footage for an automated short-form video editor.
Describe only what is visible. Do not identify or name real people; describe them generically
(e.g. "a person in a gray jacket") unless a name is printed on screen. Read on-screen text if present.
Flag watermarks, logos overlaid by stock sites, or blurred/low-quality frames. Score quality (sharpness,
exposure, stability cues) and visual interest (composition, motion cues, subject clarity) from 0 to 100.
Return only a JSON object that matches the requested schema."""


def _user_prompt(scene: Scene) -> str:
    payload: dict[str, Any] = {
        "scene_id": scene.scene_id,
        "source_file": scene.source_file,
        "duration_seconds": scene.duration,
        "frames_provided": len(scene.frames),
    }
    if scene.is_photo:
        payload["media"] = "photograph"
        return (
            "This is a still photograph (not a video frame); the editor will add a slow camera move later, "
            "so describe the picture itself. Analyze it and return the JSON object.\n" + json.dumps(payload)
        )
    return (
        "These frames are from one scene (middle frame first, then earlier/later frames if provided). "
        "Analyze the scene and return the JSON object.\n" + json.dumps(payload)
    )


def heuristic_analysis(scene: Scene) -> dict[str, Any]:
    """Used with --skip-vision: neutral scores, description from the file name."""
    stem = Path(scene.source_file).stem.replace("_", " ").replace("-", " ")
    what = "Photo" if scene.is_photo else "Scene"
    return {
        "scene_id": scene.scene_id,
        "description": f"{what} from {stem} ({scene.duration:.1f}s). Vision analysis skipped.",
        "subjects": [],
        "objects": [],
        "environment": "",
        "shot_type": "",
        "camera_motion": "",
        "subject_motion": "",
        "mood": "",
        "colors": [],
        "quality_score": 60,
        "visual_interest_score": 50,
        "watermark_detected": False,
        "text_detected": False,
        "text_content": "",
        "product_or_brand": [],
        "possible_topics": [stem],
        "safe_to_use": True,
    }


def photo_framing_analysis(scene: Scene, analysis: dict[str, Any]) -> dict[str, Any]:
    """Describe a framing of a photo from the analysis of the whole photo.

    Nothing moves in a photograph, so both motion fields read "static"; the move
    the edit adds is carried by the inventory row's ``framing`` instead (keeping
    it out of these fields also keeps the near-duplicate grouping honest, which
    compares content words). Secondary framings (detail, reveal) get a prefix so
    the writer knows they show part of the same picture.
    """
    out = dict(analysis)
    out["scene_id"] = scene.scene_id
    out["camera_motion"] = "static"
    out["subject_motion"] = "static"
    if not scene.primary_framing:
        label = FRAMING_LABELS.get(scene.framing or "", "camera move added in the edit")
        out["description"] = f"{label.capitalize()}: {analysis.get('description', '')}".strip()
    return out


def rejection_reason(analysis: dict[str, Any], cfg: Config) -> str | None:
    if not analysis.get("safe_to_use", True):
        return "flagged not safe to use"
    if bool(cfg.get("vision.reject_watermarks", True)) and analysis.get("watermark_detected"):
        return "watermark detected"
    if float(analysis.get("quality_score", 0)) < float(cfg.get("vision.min_quality_score", 40)):
        return f"quality score {analysis.get('quality_score')} below minimum"
    if float(analysis.get("visual_interest_score", 0)) < float(cfg.get("vision.min_visual_interest_score", 30)):
        return f"visual interest {analysis.get('visual_interest_score')} below minimum"
    return None


def analyze_scenes(
    doc: dict[str, Any],
    scenes: list[Scene],
    paths: JobPaths,
    vision: VisionProvider,
    cfg: Config,
    *,
    cache: JsonCache,
    skip_vision: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Fill ``scene.analysis`` for every scene; write scenes.json; return the doc."""
    model = str(cfg.get("vision.model", ""))
    max_frames = int(cfg.get("vision.max_frames_per_scene", 2))
    max_tokens = int(cfg.get("vision.max_tokens", 4000))
    calls = 0
    hits = 0
    # One vision request per photo: its primary framing is analyzed, the other framings
    # of the same picture inherit the result (they show a region of the very same image).
    photo_primary: dict[str, Scene] = {}
    for scene in scenes:
        if skip_vision:
            scene.analysis = photo_framing_analysis(scene, heuristic_analysis(scene)) if scene.is_photo else heuristic_analysis(scene)
            scene.rejected_reason = None
            continue
        if scene.is_photo and not scene.primary_framing:
            parent = photo_primary.get(scene.source_file)
            if parent is None or parent.analysis is None:
                scene.analysis = None
                scene.rejected_reason = (parent.rejected_reason if parent else None) or "whole-photo analysis unavailable"
            else:
                scene.analysis = photo_framing_analysis(scene, parent.analysis)
                scene.rejected_reason = parent.rejected_reason
            continue
        frames = [paths.work / f for f in scene.frames][:max_frames]
        frame_hashes = [hash_file(f, fast=False) for f in frames if f.exists()]
        key = make_key("vision", scene.content_hash, frame_hashes, vision.name, model, PROMPT_VERSION)
        cached = None if force else cache.get(key)
        if cached is not None:
            analysis = dict(cached)
            hits += 1
        else:
            if not frames:
                scene.analysis = None
                scene.rejected_reason = "no frames extracted"
                continue
            try:
                result = vision.analyze_images(
                    system=SYSTEM_PROMPT,
                    user=_user_prompt(scene),
                    images=frames,
                    output_schema=SCENE_ANALYSIS_SCHEMA,
                    max_tokens=max_tokens,
                )
            except ProviderError as exc:
                log.error("scene %03d: vision failed: %s", scene.scene_id, exc)
                scene.analysis = None
                scene.rejected_reason = f"vision analysis failed: {exc}"
                continue
            analysis = dict(result.data)
            calls += 1
            analysis["scene_id"] = scene.scene_id
            try:
                validate(analysis, "scene_analysis")
            except SchemaValidationError as exc:
                log.error("scene %03d: analysis failed schema: %s", scene.scene_id, exc)
                scene.analysis = None
                scene.rejected_reason = "vision output failed schema validation"
                continue
            cache.put(key, analysis)
        analysis["scene_id"] = scene.scene_id
        if scene.is_photo:
            analysis = photo_framing_analysis(scene, analysis)
            photo_primary[scene.source_file] = scene
        scene.analysis = analysis
        scene.rejected_reason = rejection_reason(analysis, cfg)
        if scene.rejected_reason:
            log.info("scene %03d rejected: %s", scene.scene_id, scene.rejected_reason)
    log.info("Vision: %d API call(s), %d cache hit(s), %d usable scene(s)", calls, hits, sum(1 for s in scenes if s.usable))
    doc["scenes"] = [s.to_dict() for s in scenes]
    validate(doc, "scenes")
    write_json(paths.scenes_json, doc)
    return doc
