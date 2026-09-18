from pathlib import Path

from autoeditor.footage_only.scenes import Scene
from autoeditor.footage_only.timeline import EPS, build_timeline, check_timeline_math, repeat_violations
from autoeditor.pipeline.job import JobPaths
from autoeditor.pipeline.voice import LineTiming


def make_scenes(spec: list[tuple[str, float]]) -> list[Scene]:
    scenes = []
    t: dict[str, float] = {}
    for i, (src, dur) in enumerate(spec, start=1):
        start = t.get(src, 0.0)
        scenes.append(
            Scene(
                scene_id=i,
                source_file=src,
                normalized_file=f"normalized/{src}",
                start_time=start,
                end_time=start + dur,
                duration=dur,
                frames=[f"frames/scene_{i:03d}_50.jpg"],
                content_hash=f"h{i}",
                analysis={"quality_score": 80, "visual_interest_score": 60},
            )
        )
        t[src] = start + dur
    return scenes


def make_inventory(scenes: list[Scene]) -> dict:
    rows = [
        {
            "scene_id": s.scene_id,
            "source_file": s.source_file,
            "duration": s.duration,
            "score": 70 + s.scene_id,
            "similar_to": [o.scene_id for o in scenes if o.source_file == s.source_file and o.scene_id != s.scene_id],
        }
        for s in scenes
    ]
    return {"usable_scenes": rows, "strongest_scenes": [{"scene_id": scenes[0].scene_id, "score": 99}]}


def make_timings(durations: list[float], gap: float = 0.1) -> list[LineTiming]:
    out = []
    cursor = 0.0
    for i, d in enumerate(durations, start=1):
        out.append(LineTiming(line_id=i, start=round(cursor, 3), end=round(cursor + d, 3), duration=d, file=f"audio/line_{i:03d}.wav"))
        cursor += d + gap
    return out


def _paths(cfg, tmp_path: Path) -> JobPaths:
    p = JobPaths.for_job(cfg, "tl")
    p.ensure()
    return p


def test_segments_tile_each_slot_exactly(cfg, tmp_path: Path) -> None:
    scenes = make_scenes([("a.mp4", 3.0), ("a.mp4", 3.0), ("b.mp4", 4.0), ("c.mp4", 2.5)])
    script = {
        "lines": [
            {"id": 1, "narration": "x", "scene_ids": [1, 3, 4], "overlay_text": None, "emphasis_words": []},
            {"id": 2, "narration": "y", "scene_ids": [2], "overlay_text": None, "emphasis_words": []},
        ]
    }
    timings = make_timings([5.8, 2.9])
    tl = build_timeline(script, timings, scenes, make_inventory(scenes), _paths(cfg, tmp_path), cfg, voice_duration=timings[-1].end)
    check_timeline_math(tl)
    line1 = tl["lines"][0]
    assert abs(sum(s["end"] - s["start"] for s in line1["segments"]) - 5.8) < EPS
    assert [s["scene_id"] for s in line1["segments"]][:3] == [1, 3, 4]
    for seg in line1["segments"]:
        assert abs((seg["source_end"] - seg["source_start"]) - (seg["end"] - seg["start"])) < EPS  # 1x speed, never stretched
    assert tl["duration"] >= timings[-1].end + 0.5  # outro tail
    assert tl["lines"][-1]["end"] == tl["duration"]


def test_short_clip_is_not_stretched_and_fallbacks_fill(cfg, tmp_path: Path) -> None:
    scenes = make_scenes([("a.mp4", 1.5), ("b.mp4", 6.0), ("b.mp4", 6.0)])
    script = {"lines": [{"id": 1, "narration": "x", "scene_ids": [1], "overlay_text": None, "emphasis_words": []}]}
    timings = make_timings([5.0])
    tl = build_timeline(script, timings, scenes, make_inventory(scenes), _paths(cfg, tmp_path), cfg, voice_duration=5.0)
    check_timeline_math(tl)
    segs = tl["lines"][0]["segments"]
    assert segs[0]["scene_id"] == 1 and abs(segs[0]["end"] - segs[0]["start"] - 1.5) < EPS
    assert len(segs) >= 2 and segs[1]["fallback"] is not None
    assert all(s["type"] == "video" for s in segs)


def test_still_frame_when_footage_exhausted(cfg, tmp_path: Path) -> None:
    scenes = make_scenes([("a.mp4", 2.0)])
    script = {
        "lines": [
            {"id": 1, "narration": "x", "scene_ids": [1], "overlay_text": None, "emphasis_words": []},
            {"id": 2, "narration": "y", "scene_ids": [1], "overlay_text": None, "emphasis_words": []},
        ]
    }
    timings = make_timings([2.0, 3.0])
    tl = build_timeline(script, timings, scenes, make_inventory(scenes), _paths(cfg, tmp_path), cfg, voice_duration=timings[-1].end)
    check_timeline_math(tl)
    kinds = [(s["type"], s["fallback"]) for line in tl["lines"] for s in line["segments"]]
    assert ("image", "still_frame") in kinds  # never black
    assert all(s["end"] > s["start"] for line in tl["lines"] for s in line["segments"])


def test_repeat_protection_prefers_other_scenes(cfg, tmp_path: Path) -> None:
    scenes = make_scenes([("a.mp4", 4.0), ("a.mp4", 4.0), ("b.mp4", 4.0), ("b.mp4", 4.0), ("c.mp4", 4.0), ("c.mp4", 4.0)])
    # Every line asks for scene 1 only; the builder must spread usage.
    script = {"lines": [{"id": i, "narration": "x", "scene_ids": [1], "overlay_text": None, "emphasis_words": []} for i in range(1, 6)]}
    timings = make_timings([3.0] * 5)
    tl = build_timeline(script, timings, scenes, make_inventory(scenes), _paths(cfg, tmp_path), cfg, voice_duration=timings[-1].end)
    check_timeline_math(tl)
    used = [s["scene_id"] for line in tl["lines"] for s in line["segments"] if s["type"] == "video"]
    assert len(set(used)) >= 4
    assert repeat_violations(tl, float(cfg.get("timeline.repeat_window_seconds", 30))) == []


def test_check_timeline_math_detects_gaps_and_stretch() -> None:
    base = {
        "duration": 4.0,
        "lines": [
            {
                "line_id": 1,
                "start": 0.0,
                "end": 4.0,
                "segments": [
                    {"src": "a", "type": "video", "start": 0.0, "end": 2.0, "source_start": 0.0, "source_end": 2.0},
                    {"src": "a", "type": "video", "start": 2.5, "end": 4.0, "source_start": 2.0, "source_end": 3.5},
                ],
            }
        ],
    }
    try:
        check_timeline_math(base)
        raise AssertionError("gap not detected")
    except ValueError:
        pass
    stretched = {
        "duration": 4.0,
        "lines": [
            {
                "line_id": 1,
                "start": 0.0,
                "end": 4.0,
                "segments": [{"src": "a", "type": "video", "start": 0.0, "end": 4.0, "source_start": 0.0, "source_end": 2.0}],
            }
        ],
    }
    try:
        check_timeline_math(stretched)
        raise AssertionError("stretch not detected")
    except ValueError:
        pass
    stretched["lines"][0]["segments"][0]["fallback"] = "loop"
    check_timeline_math(stretched, allow_loop=True)
