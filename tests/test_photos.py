"""Native photo support: discovery, normalization, framings, vision sharing, timeline, e2e."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image

from autoeditor.cache import JsonCache
from autoeditor.footage_only.discovery import discover_job, discover_jobs, media_kind
from autoeditor.footage_only.inventory import build_inventory
from autoeditor.footage_only.normalize import normalize_clips
from autoeditor.footage_only.photos import (
    FRAMING_DETAIL,
    FRAMING_PAN,
    FRAMING_PUSH,
    FRAMING_REVEAL,
    PHOTO_GRACE_SECONDS,
    PhotoInfo,
    clamp_focus,
    detail_focus,
    photo_budget_seconds,
    plan_framings,
    probe_photo,
    slice_motion,
    validate_photo,
    visible_box,
)
from autoeditor.footage_only.runner import FootageOnlyOptions, run_footage_only
from autoeditor.footage_only.scenes import Scene, build_scenes, scenes_from_doc
from autoeditor.footage_only.timeline import EPS, build_timeline, check_timeline_math
from autoeditor.footage_only.vision import analyze_scenes
from autoeditor.pipeline.job import JobPaths
from autoeditor.providers.mock import MockVision
from autoeditor.schemas import validate
from tests.conftest import make_photo, requires_ffmpeg
from tests.test_timeline import make_timings

FRAME = {"frame_width": 270, "frame_height": 480}


# --------------------------------------------------------------------------- #
# Discovery + normalization
# --------------------------------------------------------------------------- #
def test_media_kind_and_discovery(photo_inbox: Path, cfg) -> None:
    assert media_kind(Path("a.JPG"), cfg) == "image"
    assert media_kind(Path("a.mp4"), cfg) == "video"
    assert media_kind(Path("a.docx"), cfg) is None
    job = discover_job(photo_inbox / "photos_only", cfg)
    assert [p.name for p in job.photos] == ["badge.png", "car_road.jpg", "hood.jpg", "seats.webp", "tiny.jpg", "wheel.jpg"]
    assert job.videos == []
    assert job.kinds["badge.png"] == "image"
    by_file = {c.file: c for c in job.credits}
    assert by_file["car_road.jpg"].kind == "image" and by_file["car_road.jpg"].license == "Press use"
    jobs = discover_jobs(photo_inbox, cfg)
    assert [j.name for j in jobs] == ["mixed", "photos_only"]


def test_photo_normalization_orients_strips_metadata_and_keeps_originals(photo_inbox: Path, cfg) -> None:
    job_dir = photo_inbox / "photos_only"
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in job_dir.iterdir()}
    job = discover_job(job_dir, cfg)
    paths = JobPaths.for_job(cfg, job.name, inbox=job_dir)
    paths.ensure()
    clips = normalize_clips(job, paths, cfg)
    by_name = {c.file: c for c in clips}
    assert by_name["tiny.jpg"].status == "rejected" and "resolution" in (by_name["tiny.jpg"].reason or "")
    hood = by_name["hood.jpg"]
    assert hood.usable and hood.is_photo and hood.normalized_duration is None
    # EXIF orientation 6 is applied: the stored 480x640 file displays as 640x480.
    assert hood.info["orientation_applied"] is True
    with Image.open(paths.work / hood.normalized) as im:
        assert im.size == (640, 480)
        assert 0x8825 not in im.getexif() and 0x0112 not in im.getexif()
    car = by_name["car_road.jpg"]
    with Image.open(job_dir / "car_road.jpg") as original:
        assert original.getexif().get_ifd(0x8825)  # the source really carries GPS data
    with Image.open(paths.work / car.normalized) as im:
        assert im.format == "JPEG" and not im.getexif().get_ifd(0x8825) and 0x8825 not in im.getexif()  # GPS stripped
    assert all(by_name[n].usable for n in ("badge.png", "seats.webp", "wheel.jpg"))
    # Originals are byte-identical and a re-run reuses the normalized copies.
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in job_dir.iterdir()}
    assert before == after
    norm = paths.work / car.normalized
    m1 = norm.stat().st_mtime_ns
    normalize_clips(job, paths, cfg)
    assert norm.stat().st_mtime_ns == m1


def test_validate_photo_thresholds(cfg) -> None:
    cfg.set("media.min_width", 480)
    cfg.set("media.min_height", 480)
    ok = PhotoInfo(filename="a.jpg", path="a.jpg", width=1280, height=960, format="JPEG", mode="RGB")
    assert validate_photo(ok, cfg) is None
    small = PhotoInfo(filename="a.jpg", path="a.jpg", width=320, height=240, format="JPEG", mode="RGB")
    assert "resolution" in (validate_photo(small, cfg) or "")


def test_probe_photo_handles_max_edge_downscale(tmp_path: Path) -> None:
    from autoeditor.footage_only.photos import normalize_photo

    big = make_photo(tmp_path / "big.png", size=(1600, 1200))
    info = normalize_photo(big, tmp_path / "out.jpg", max_edge=800, quality=85)
    assert (info.width, info.height) == (800, 600)
    small = make_photo(tmp_path / "small.png", size=(300, 200))
    info2 = normalize_photo(small, tmp_path / "out2.jpg", max_edge=800, quality=85)
    assert (info2.width, info2.height) == (300, 200)  # never upscaled
    assert probe_photo(big).width == 1600


# --------------------------------------------------------------------------- #
# Camera paths (pure)
# --------------------------------------------------------------------------- #
def _inside(state: dict[str, float], w: int, h: int) -> bool:
    x0, y0, x1, y1 = visible_box(state, width=w, height=h, **FRAME)
    return 0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h


def test_plan_framings_landscape_pans_and_portrait_pushes() -> None:
    land = plan_framings(1280, 960, index=0, hold=4.0, focus=(0.7, 0.6), **FRAME)
    assert [m["framing"] for m in land] == [FRAMING_PAN, FRAMING_DETAIL, FRAMING_REVEAL]
    pan = land[0]
    assert pan["primary"] is True and pan["src_width"] == 1280 and pan["hold_seconds"] == 4.0
    assert pan["to"]["x"] > pan["from"]["x"]  # even index pans left -> right
    assert plan_framings(1280, 960, index=1, hold=4.0, focus=(0.5, 0.5), **FRAME)[0]["to"]["x"] < 0.5
    # A 3:4 photo is still a third wider than a 9:16 frame: it pans. A 9:16 photo pushes.
    assert plan_framings(960, 1280, index=0, hold=4.0, focus=(0.5, 0.3), **FRAME)[0]["framing"] == FRAMING_PAN
    portrait = plan_framings(720, 1280, index=0, hold=4.0, focus=(0.5, 0.3), **FRAME)
    assert portrait[0]["framing"] == FRAMING_PUSH and portrait[0]["to"]["scale"] > portrait[0]["from"]["scale"]
    assert plan_framings(720, 1280, index=1, hold=4.0, focus=(0.5, 0.3), **FRAME)[0]["to"]["scale"] < 1.05  # odd index pulls out
    detail = land[1]
    assert detail["primary"] is False and detail["from"]["scale"] < detail["to"]["scale"] and detail["to"]["scale"] > 1.3
    reveal = land[2]
    assert reveal["from"]["scale"] > reveal["to"]["scale"]
    assert len(plan_framings(1280, 960, index=0, hold=4.0, focus=(0.5, 0.5), count=1, **FRAME)) == 1
    assert len(plan_framings(1280, 960, index=0, hold=4.0, focus=(0.5, 0.5), count=9, **FRAME)) == 3
    for photo_w, photo_h, framings in ((1280, 960, land), (720, 1280, portrait)):
        for m in framings:
            for state in (m["from"], m["to"]):
                assert state["scale"] >= 1.0 and 0.0 <= state["x"] <= 1.0 and 0.0 <= state["y"] <= 1.0
                assert _inside(state, photo_w, photo_h)


def test_clamp_focus_keeps_the_window_inside_the_photo() -> None:
    # At scale 1 a landscape photo leaves horizontal room only; the vertical focus is pinned to the centre.
    x, y = clamp_focus(0.0, 0.0, width=1280, height=960, scale=1.0, **FRAME)
    assert 0.0 < x < 0.5 and y == 0.5
    # A square-ish window that already covers everything cannot move at all.
    x, y = clamp_focus(0.9, 0.1, width=270, height=480, scale=1.0, **FRAME)
    assert (x, y) == (0.5, 0.5)
    # More zoom means more room to move.
    x_wide, _ = clamp_focus(0.0, 0.5, width=1280, height=960, scale=1.5, **FRAME)
    assert x_wide < clamp_focus(0.0, 0.5, width=1280, height=960, scale=1.0, **FRAME)[0]


def test_slice_motion_is_linear_for_partial_paths_and_rests_past_the_hold() -> None:
    motion = {"framing": "pan", "from": {"scale": 1.0, "x": 0.3, "y": 0.5}, "to": {"scale": 1.1, "x": 0.7, "y": 0.5}}
    whole = slice_motion(motion, 0.0, 4.0, 4.0)
    assert whole["ease"] == "inout" and whole["from"] == {"scale": 1.0, "x": 0.3, "y": 0.5} and whole["to"] == {"scale": 1.1, "x": 0.7, "y": 0.5}
    first = slice_motion(motion, 0.0, 2.0, 4.0)
    second = slice_motion(motion, 2.0, 4.0, 4.0)
    assert first["ease"] == "linear" and first["to"] == second["from"] == {"scale": 1.05, "x": 0.5, "y": 0.5}
    tail = slice_motion(motion, 3.0, 5.0, 4.0)  # runs 1s past the hold: rests at the end state
    assert tail["to"] == {"scale": 1.1, "x": 0.7, "y": 0.5}


def test_detail_focus_finds_the_textured_region(tmp_path: Path) -> None:
    p = make_photo(tmp_path / "corner.png", size=(640, 480), detail_at=(0.8, 0.75))
    fx, fy = detail_focus(p)
    assert fx > 0.6 and fy > 0.6
    q = make_photo(tmp_path / "left.png", size=(640, 480), detail_at=(0.2, 0.3))
    fx2, fy2 = detail_focus(q)
    assert fx2 < 0.4 and fy2 < 0.5
    assert detail_focus(tmp_path / "missing.png") == (0.5, 0.5)


# --------------------------------------------------------------------------- #
# Scenes, vision sharing, inventory
# --------------------------------------------------------------------------- #
class CountingVision:
    name = "mock"

    def __init__(self) -> None:
        self.inner = MockVision()
        self.calls = 0

    def analyze_images(self, **kw: Any) -> Any:
        self.calls += 1
        return self.inner.analyze_images(**kw)


def _analyzed_photo_job(photo_inbox: Path, cfg, *, name: str = "photos_only") -> tuple[JobPaths, dict[str, Any], list[Scene], CountingVision]:
    job = discover_job(photo_inbox / name, cfg)
    paths = JobPaths.for_job(cfg, job.name, inbox=job.inbox_dir)
    paths.ensure()
    clips = normalize_clips(job, paths, cfg)
    doc = build_scenes(clips, paths, cfg)
    vision = CountingVision()
    scenes = scenes_from_doc(doc)
    doc = analyze_scenes(doc, scenes, paths, vision, cfg, cache=JsonCache(paths.cache / "vision"))
    return paths, doc, scenes, vision


def test_photo_scenes_carry_motion_frames_and_validate(photo_inbox: Path, cfg) -> None:
    paths, doc, scenes, _ = _analyzed_photo_job(photo_inbox, cfg)
    validate(doc, "scenes")
    photos = [s for s in scenes if s.is_photo]
    assert len(photos) == 5 * 3  # five usable photos x three framings
    assert {s.framing for s in photos} == {FRAMING_PAN, FRAMING_PUSH, FRAMING_DETAIL, FRAMING_REVEAL}
    for s in photos:
        assert s.motion is not None and s.duration == float(cfg.get("media.photo_hold_seconds"))
        assert len(s.frames) == 1 and (paths.work / s.frames[0]).exists()
        assert s.normalized_file.endswith(".jpg")
    # The rotated photo is planned with its displayed (landscape) dimensions.
    hood = next(s for s in photos if s.source_file == "hood.jpg" and s.primary_framing)
    assert (hood.motion["src_width"], hood.motion["src_height"]) == (640, 480) and hood.framing == FRAMING_PAN
    badge = next(s for s in photos if s.source_file == "badge.png" and s.primary_framing)
    assert badge.framing == FRAMING_PAN  # a square photo is much wider than 9:16: pan across it
    seats = next(s for s in photos if s.source_file == "seats.webp" and s.primary_framing)
    assert seats.framing == FRAMING_PUSH  # a 9:16 photo fills the frame: push, not pan
    # Detail frames are crops (smaller than the full-photo frame's aspect would give).
    detail = next(s for s in photos if s.source_file == "car_road.jpg" and s.framing == FRAMING_DETAIL)
    with Image.open(paths.work / detail.frames[0]) as im:
        assert im.size[0] <= int(cfg.get("media.frame_width"))


def test_vision_is_called_once_per_photo_and_shared_by_its_framings(photo_inbox: Path, cfg) -> None:
    _, _, scenes, vision = _analyzed_photo_job(photo_inbox, cfg)
    assert vision.calls == 5
    by_source: dict[str, list[Scene]] = {}
    for s in scenes:
        by_source.setdefault(s.source_file, []).append(s)
    for group in by_source.values():
        primary = next(s for s in group if s.primary_framing)
        assert primary.analysis is not None and primary.analysis["camera_motion"] == "static" and primary.analysis["subject_motion"] == "static"
        for s in group:
            assert s.usable and s.analysis is not None
            assert s.analysis["quality_score"] == primary.analysis["quality_score"]
            if not s.primary_framing:
                assert s.analysis["description"].lower().startswith(("slow push-in", "zoom-out reveal"))
    inventory = build_inventory(scenes, cfg)
    assert inventory["media"]["photo_sources"] == 5 and inventory["media"]["video_sources"] == 0
    assert inventory["total_usable_seconds"] == 5 * photo_budget_seconds(cfg)
    assert any("still photographs" in n for n in inventory["notes"])
    assert all(row["kind"] == "image" and row["framing"] for row in inventory["usable_scenes"])
    # Framings of one photo are near-duplicates: one group per photo, and the strongest list spans photos.
    assert len(inventory["duplicate_groups"]) == 5
    assert len({row["scene_id"] for row in inventory["strongest_scenes"]}) >= 3


def test_rejected_photo_rejects_all_of_its_framings(photo_inbox: Path, cfg, monkeypatch) -> None:
    cfg.set("vision.min_quality_score", 200)  # nothing can pass
    _, _, scenes, vision = _analyzed_photo_job(photo_inbox, cfg)
    assert vision.calls == 5
    assert all(not s.usable for s in scenes)
    secondary = [s for s in scenes if not s.primary_framing]
    assert secondary and all("quality score" in (s.rejected_reason or "") for s in secondary)


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #
def _photo_scenes(hold: float = 4.0) -> list[Scene]:
    scenes: list[Scene] = []
    sid = 1
    for index, (name, w, h) in enumerate((("a.jpg", 1280, 960), ("b.jpg", 720, 1280))):
        for motion in plan_framings(w, h, index=index, hold=hold, focus=(0.6, 0.5), **FRAME):
            scenes.append(
                Scene(
                    scene_id=sid,
                    source_file=name,
                    normalized_file=f"normalized/0{index + 1}_{name}",
                    start_time=0.0,
                    end_time=hold,
                    duration=hold,
                    frames=[f"frames/scene_{sid:03d}_50.jpg"],
                    content_hash=f"p{sid}",
                    analysis={"quality_score": 80, "visual_interest_score": 60},
                    kind="image",
                    motion=motion,
                )
            )
            sid += 1
    return scenes


def _inventory(scenes: list[Scene]) -> dict[str, Any]:
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
    return {"usable_scenes": rows, "strongest_scenes": [{"scene_id": scenes[0].scene_id, "score": 99}, {"scene_id": scenes[3].scene_id, "score": 90}]}


def test_photo_timeline_plays_camera_paths_at_1x_within_budget(cfg, tmp_path: Path) -> None:
    scenes = _photo_scenes()
    script = {
        "lines": [
            {"id": 1, "narration": "x", "scene_ids": [1, 4], "overlay_text": None, "emphasis_words": []},
            {"id": 2, "narration": "y", "scene_ids": [2], "overlay_text": None, "emphasis_words": []},
            {"id": 3, "narration": "z", "scene_ids": [1], "overlay_text": None, "emphasis_words": []},
        ]
    }
    timings = make_timings([6.0, 3.0, 5.0])
    paths = JobPaths.for_job(cfg, "photo_tl")
    paths.ensure()
    tl = build_timeline(script, timings, scenes, _inventory(scenes), paths, cfg, voice_duration=timings[-1].end)
    validate(tl, "timeline")
    check_timeline_math(tl)
    segs = [s for line in tl["lines"] for s in line["segments"]]
    assert segs and all(s["type"] == "image" and s["effect"] == "kenburns" for s in segs)
    assert all(s["motion"] and s["motion"]["framing"] for s in segs)
    assert all(s["src"].startswith("normalized/") for s in segs)  # the photo itself, never a thumbnail
    hold = float(cfg.get("media.photo_hold_seconds"))
    consumed: dict[int, float] = {}
    for s in segs:
        if s["fallback"] != "still_frame":
            assert abs((s["source_end"] - s["source_start"]) - (s["end"] - s["start"])) < EPS  # 1x along the path
            consumed[s["scene_id"]] = consumed.get(s["scene_id"], 0.0) + (s["source_end"] - s["source_start"])
    assert all(v <= hold + PHOTO_GRACE_SECONDS + EPS for v in consumed.values())
    # A framing reused later continues its move instead of restarting it.
    uses = [s for s in segs if s["scene_id"] == 1 and s["fallback"] != "still_frame"]
    if len(uses) >= 2:
        assert uses[1]["source_start"] >= uses[0]["source_end"] - EPS
        assert uses[1]["motion"]["from"] == uses[0]["motion"]["to"]


def test_mixed_scenes_keep_video_segments_untouched(cfg, tmp_path: Path) -> None:
    from tests.test_timeline import make_inventory, make_scenes

    video = make_scenes([("clip.mp4", 3.0), ("clip.mp4", 3.0)])
    photos = _photo_scenes()
    for i, s in enumerate(photos, start=len(video) + 1):
        s.scene_id = i
    scenes = video + photos
    inventory = make_inventory(scenes)
    script = {"lines": [{"id": 1, "narration": "x", "scene_ids": [1, 3], "overlay_text": None, "emphasis_words": []}]}
    timings = make_timings([5.5])
    paths = JobPaths.for_job(cfg, "mixed_tl")
    paths.ensure()
    tl = build_timeline(script, timings, scenes, inventory, paths, cfg, voice_duration=5.5)
    check_timeline_math(tl)
    segs = tl["lines"][0]["segments"]
    assert segs[0]["type"] == "video" and segs[0]["effect"] == "none" and "motion" not in segs[0]
    assert any(s["type"] == "image" and s.get("motion") for s in segs)


# --------------------------------------------------------------------------- #
# End to end (mock providers)
# --------------------------------------------------------------------------- #
def test_photo_only_job_end_to_end(photo_inbox: Path, cfg) -> None:
    results = run_footage_only(cfg, FootageOnlyOptions(inbox=photo_inbox, job="photos_only", skip_render=True))
    assert results[0].status == "skipped_render", results[0].message
    paths = JobPaths.for_job(cfg, "photos_only")
    timeline = json.loads(paths.timeline_json.read_text(encoding="utf-8"))
    validate(timeline, "timeline")
    check_timeline_math(timeline)
    segs = [s for line in timeline["lines"] for s in line["segments"]]
    assert segs and all(s["type"] == "image" and s.get("motion") for s in segs)
    manifest = json.loads(paths.media_manifest_json.read_text(encoding="utf-8"))
    assert {c["kind"] for c in manifest["clips"]} == {"image"}
    credits = paths.credits_txt.read_text(encoding="utf-8")
    assert "IMAGES" in credits and "Press use" in credits and "LICENSE_UNKNOWN" in credits
    assert "FOOTAGE" not in credits
    assert paths.thumbnail_jpg.exists()
    script = json.loads(paths.script_json.read_text(encoding="utf-8"))
    assert script["topic"] == "what makes this SUV look expensive"
    # Resume: nothing is recomputed on a second run.
    mtimes = {p: p.stat().st_mtime_ns for p in (paths.scenes_json, paths.script_json, paths.timeline_json)}
    assert run_footage_only(cfg, FootageOnlyOptions(inbox=photo_inbox, job="photos_only", skip_render=True))[0].status == "skipped_render"
    assert {p: p.stat().st_mtime_ns for p in mtimes} == mtimes


@requires_ffmpeg
def test_mixed_job_end_to_end(photo_inbox: Path, cfg) -> None:
    results = run_footage_only(cfg, FootageOnlyOptions(inbox=photo_inbox, job="mixed", skip_render=True, skip_vision=True, skip_voice=True))
    assert results[0].status == "skipped_render", results[0].message
    paths = JobPaths.for_job(cfg, "mixed")
    timeline = json.loads(paths.timeline_json.read_text(encoding="utf-8"))
    check_timeline_math(timeline)
    segs = [s for line in timeline["lines"] for s in line["segments"]]
    kinds = {s["type"] for s in segs}
    assert kinds == {"video", "image"}
    assert all(s.get("motion") for s in segs if s["type"] == "image" and s["fallback"] != "still_frame")
    inventory = json.loads(paths.inventory_json.read_text(encoding="utf-8"))
    assert inventory["media"]["video_sources"] == 2 and inventory["media"]["photo_sources"] == 2
    credits = paths.credits_txt.read_text(encoding="utf-8")
    assert "FOOTAGE" in credits and "IMAGES" in credits


@requires_ffmpeg
def test_dry_run_counts_photos(photo_inbox: Path, cfg) -> None:
    results = run_footage_only(cfg, FootageOnlyOptions(inbox=photo_inbox, dry_run=True))
    by_name = {r.name: r for r in results}
    assert by_name["photos_only"].status == "dry_run" and "5 usable" in by_name["photos_only"].message
    assert by_name["mixed"].status == "dry_run" and "4 usable" in by_name["mixed"].message
    assert not (cfg.work_dir / "photos_only").exists()
