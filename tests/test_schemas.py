import pytest

from autoeditor.schemas import SchemaValidationError, is_valid, validate

VALID_ANALYSIS = {
    "scene_id": 1,
    "description": "Close-up of a phone on a desk.",
    "subjects": ["phone"],
    "objects": ["desk"],
    "environment": "studio",
    "shot_type": "close-up",
    "camera_motion": "static",
    "subject_motion": "none",
    "mood": "sleek",
    "colors": ["black", "silver"],
    "quality_score": 88,
    "visual_interest_score": 70,
    "watermark_detected": False,
    "text_detected": False,
    "text_content": "",
    "product_or_brand": ["iPhone"],
    "possible_topics": ["phone design"],
    "safe_to_use": True,
}


def test_scene_analysis_valid() -> None:
    validate(VALID_ANALYSIS, "scene_analysis")


def test_scene_analysis_rejects_out_of_range_and_extra_keys() -> None:
    bad = dict(VALID_ANALYSIS, quality_score=140)
    with pytest.raises(SchemaValidationError) as exc:
        validate(bad, "scene_analysis")
    assert "quality_score" in str(exc.value)
    assert not is_valid(dict(VALID_ANALYSIS, extra="nope"), "scene_analysis")
    missing = dict(VALID_ANALYSIS)
    missing.pop("safe_to_use")
    assert not is_valid(missing, "scene_analysis")


def test_scenes_file_schema() -> None:
    doc = {
        "version": 1,
        "job": "j",
        "sources": [],
        "scenes": [
            {
                "scene_id": 1,
                "source_file": "a.mp4",
                "normalized_file": "normalized/01_a.mp4",
                "start_time": 0.0,
                "end_time": 2.0,
                "duration": 2.0,
                "frames": [],
                "content_hash": "abc",
                "analysis": VALID_ANALYSIS,
                "rejected_reason": None,
            }
        ],
    }
    validate(doc, "scenes")
    doc["scenes"][0]["analysis"] = {"scene_id": 1}
    assert not is_valid(doc, "scenes")


def test_script_schema() -> None:
    script = {
        "version": 1,
        "mode": "footage_only",
        "title": "T",
        "description": "D",
        "topic": None,
        "theme": "default",
        "facts_to_verify": [],
        "lines": [{"id": 1, "narration": "Hello.", "scene_ids": [1], "overlay_text": None, "emphasis_words": []}],
    }
    validate(script, "script")
    assert not is_valid(dict(script, lines=[]), "script")
    assert not is_valid(dict(script, mode="weird"), "script")
    assert not is_valid({"title": "T", "description": "D"}, "script")


def test_captions_and_timeline_schemas() -> None:
    captions = {
        "version": 1,
        "language": "en",
        "duration": 2.0,
        "words": [{"text": "hi", "start": 0.0, "end": 0.4, "line_id": 1, "emphasis": False}],
        "segments": [{"text": "hi", "start": 0.0, "end": 0.4, "words": [{"text": "hi", "start": 0.0, "end": 0.4}]}],
    }
    validate(captions, "captions")
    timeline = {
        "version": 1,
        "fps": 30,
        "width": 1080,
        "height": 1920,
        "duration": 2.0,
        "voice": "audio/voice.mp3",
        "music": None,
        "sfx": [],
        "lines": [
            {
                "line_id": 1,
                "start": 0.0,
                "end": 2.0,
                "segments": [
                    {
                        "src": "normalized/a.mp4",
                        "type": "video",
                        "start": 0.0,
                        "end": 2.0,
                        "source_start": 0.0,
                        "source_end": 2.0,
                        "scene_id": 1,
                        "effect": "none",
                        "transition": "cut",
                        "fallback": None,
                    }
                ],
            }
        ],
    }
    validate(timeline, "timeline")
    timeline["lines"][0]["segments"][0]["type"] = "gif"
    assert not is_valid(timeline, "timeline")
