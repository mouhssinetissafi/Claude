"""Regression tests for timeline quality rules found during the first mock render."""

from pathlib import Path

from autoeditor.footage_only.inventory import build_inventory
from autoeditor.footage_only.scenes import Scene
from autoeditor.footage_only.timeline import SLIVER, build_timeline, check_timeline_math
from autoeditor.pipeline.job import JobPaths
from tests.test_timeline import make_inventory, make_scenes, make_timings


def _paths(cfg) -> JobPaths:  # type: ignore[no-untyped-def]
    p = JobPaths.for_job(cfg, "tlq")
    p.ensure()
    return p


def _all_segments(tl: dict) -> list[dict]:
    return [s for line in tl["lines"] for s in line["segments"]]


def test_no_sliver_segments_when_footage_runs_out(cfg) -> None:
    # 7 scenes / 24s of footage against ~22.5s of narration: remainders get tight near the end.
    scenes = make_scenes([("a.mp4", 3.0)] * 4 + [("b.mp4", 4.0)] * 3)
    script = {
        "lines": [
            {"id": 1, "narration": "x", "scene_ids": [7, 1, 2], "overlay_text": None, "emphasis_words": []},
            {"id": 2, "narration": "x", "scene_ids": [5, 4], "overlay_text": None, "emphasis_words": []},
            {"id": 3, "narration": "x", "scene_ids": [6, 3, 7], "overlay_text": None, "emphasis_words": []},
            {"id": 4, "narration": "x", "scene_ids": [1, 2, 5], "overlay_text": None, "emphasis_words": []},
            {"id": 5, "narration": "x", "scene_ids": [4, 6], "overlay_text": None, "emphasis_words": []},
        ]
    }
    timings = make_timings([5.77, 3.84, 4.61, 4.61, 3.33])
    tl = build_timeline(script, timings, scenes, make_inventory(scenes), _paths(cfg), cfg, voice_duration=timings[-1].end)
    check_timeline_math(tl)
    for seg in _all_segments(tl):
        assert seg["end"] - seg["start"] >= SLIVER - 0.001, seg
    # Consecutive still frames are merged, never stacked.
    for line in tl["lines"]:
        kinds = [s["type"] for s in line["segments"]]
        assert not any(a == b == "image" for a, b in zip(kinds, kinds[1:], strict=False)), kinds


def test_outro_extends_image_instead_of_appending(cfg) -> None:
    scenes = make_scenes([("a.mp4", 1.0)])
    script = {"lines": [{"id": 1, "narration": "x", "scene_ids": [1], "overlay_text": None, "emphasis_words": []}]}
    timings = make_timings([3.0])
    tl = build_timeline(script, timings, scenes, make_inventory(scenes), _paths(cfg), cfg, voice_duration=3.0)
    check_timeline_math(tl)
    segs = tl["lines"][0]["segments"]
    assert [s["type"] for s in segs] == ["video", "image"]
    assert segs[-1]["end"] == tl["duration"]


def test_leftover_footage_is_preferred_over_still_frames(cfg) -> None:
    scenes = make_scenes([("a.mp4", 2.0), ("b.mp4", 0.9)])  # 0.9s leftover is short but real
    script = {"lines": [{"id": 1, "narration": "x", "scene_ids": [1], "overlay_text": None, "emphasis_words": []}]}
    timings = make_timings([2.8])
    tl = build_timeline(script, timings, scenes, make_inventory(scenes), _paths(cfg), cfg, voice_duration=2.8)
    check_timeline_math(tl)
    segs = tl["lines"][0]["segments"]
    assert segs[1]["type"] == "video" and segs[1]["scene_id"] == 2


def test_strongest_scenes_topped_up_when_all_duplicates(cfg) -> None:
    analysis = {
        "description": "same look",
        "subjects": ["phone"],
        "objects": ["desk"],
        "environment": "studio",
        "shot_type": "close-up",
        "camera_motion": "static",
        "mood": "calm",
        "colors": ["black"],
        "quality_score": 80,
        "visual_interest_score": 60,
        "product_or_brand": [],
        "possible_topics": [],
    }
    scenes = [
        Scene(
            scene_id=i,
            source_file="a.mp4",
            normalized_file="normalized/a.mp4",
            start_time=i * 2.0,
            end_time=i * 2.0 + 2.0,
            duration=2.0,
            analysis=dict(analysis, scene_id=i, visual_interest_score=50 + i),
        )
        for i in range(1, 6)
    ]
    inv = build_inventory(scenes, cfg)
    assert inv["duplicate_groups"] == [[1, 2, 3, 4, 5]]
    assert len(inv["strongest_scenes"]) == 5
    assert inv["strongest_scenes"][0]["scene_id"] == 5  # best score first


def test_credits_only_include_footage_used(cfg, inbox: Path) -> None:
    from autoeditor.footage_only.runner import FootageOnlyOptions, run_footage_only

    results = run_footage_only(cfg, FootageOnlyOptions(inbox=inbox, job="iphone_air", skip_render=True, skip_vision=True, skip_voice=True))
    assert results[0].status == "skipped_render", results[0].message
    paths = JobPaths.for_job(cfg, "iphone_air")
    credits = paths.credits_txt.read_text(encoding="utf-8")
    assert "notes.docx" not in credits
    # Both clips are short enough that the timeline uses both sources.
    assert "clip01.mp4" in credits and "clip02.mov" in credits
