from pathlib import Path

from autoeditor.footage_only.scenes import build_scenes, refine_scene_bounds, scene_content_hash
from autoeditor.schemas import validate
from tests.conftest import requires_ffmpeg


def test_refine_merges_short_and_chunks_long() -> None:
    raw = [(0.0, 0.3), (0.3, 5.0), (5.0, 30.0), (30.0, 30.5)]
    out = refine_scene_bounds(raw, 30.5, min_scene=0.8, max_scene=12.0, max_scenes=40)
    assert out[0][0] == 0.0  # tiny first scene absorbed
    assert all(e - s >= 0.8 for s, e in out)
    assert all(e - s <= 12.0 + 0.01 for s, e in out)
    assert abs(out[-1][1] - 30.5) < 0.01
    # contiguous
    for (_s1, e1), (s2, _e2) in zip(out, out[1:], strict=False):
        assert abs(e1 - s2) < 0.001


def test_refine_caps_scene_count_and_handles_empty() -> None:
    raw = [(i * 1.0, (i + 1) * 1.0) for i in range(100)]
    out = refine_scene_bounds(raw, 100.0, min_scene=0.5, max_scene=12.0, max_scenes=10)
    assert len(out) == 10
    assert refine_scene_bounds([], 5.0, min_scene=0.5, max_scene=12.0, max_scenes=10) == [(0.0, 5.0)]
    assert refine_scene_bounds([(0, 1)], 0.0, min_scene=0.5, max_scene=12.0, max_scenes=10) == []


def test_scene_content_hash_is_stable() -> None:
    assert scene_content_hash("abc", 0.0, 2.0) == scene_content_hash("abc", 0.0, 2.0)
    assert scene_content_hash("abc", 0.0, 2.0) != scene_content_hash("abc", 0.0, 2.5)
    assert scene_content_hash("abc", 0.0, 2.0) != scene_content_hash("abd", 0.0, 2.0)


@requires_ffmpeg
def test_build_scenes_detects_cuts_and_extracts_frames(cfg, inbox: Path) -> None:
    from autoeditor.footage_only.discovery import discover_job
    from autoeditor.footage_only.normalize import normalize_clips
    from autoeditor.pipeline.job import JobPaths

    job = discover_job(inbox / "no_topic", cfg)  # 3 hard cuts of solid colours
    paths = JobPaths.for_job(cfg, job.name, inbox=job.inbox_dir)
    paths.ensure()
    clips = normalize_clips(job, paths, cfg)
    doc = build_scenes(clips, paths, cfg)
    validate(doc, "scenes")
    assert len(doc["scenes"]) >= 3
    for scene in doc["scenes"]:
        assert scene["duration"] > 0
        assert len(scene["frames"]) == 3
        for f in scene["frames"]:
            assert (paths.work / f).exists()
    total = sum(s["duration"] for s in doc["scenes"])
    assert abs(total - clips[0].normalized_duration) < 0.2
