"""Watermark behaviour: logo present, absent, disabled, invalid; timeline and asset staging."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from autoeditor.pipeline.branding import logo_path, resolve_watermark, validate_logo
from autoeditor.pipeline.job import JobPaths
from autoeditor.pipeline.render import collect_asset_paths
from autoeditor.schemas import validate
from tests.conftest import requires_ffmpeg
from tests.test_timeline import make_inventory, make_scenes, make_timings


def _write_logo(path: Path, *, size: tuple[int, int] = (256, 128)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    for x in range(size[0] // 2):
        for y in range(size[1] // 2):
            img.putpixel((x, y), (255, 214, 10, 220))
    img.save(path, "PNG")
    return path


def _paths(cfg, name: str = "wm") -> JobPaths:  # type: ignore[no-untyped-def]
    p = JobPaths.for_job(cfg, name)
    p.ensure()
    return p


def test_no_logo_means_no_watermark_and_no_error(cfg) -> None:
    assert not logo_path(cfg).exists()
    assert resolve_watermark(_paths(cfg), cfg) == (None, None)


def test_logo_present_is_used_with_defaults(cfg) -> None:
    _write_logo(logo_path(cfg))
    paths = _paths(cfg)
    wm, warning = resolve_watermark(paths, cfg)
    assert warning is None and wm is not None
    assert wm["src"] == "branding/logo.png"
    assert (paths.work / wm["src"]).exists()
    assert wm["position"] == "top-right"
    assert 0 < wm["width_fraction"] <= 0.3 and 0 < wm["opacity"] <= 1
    # Transparency preserved: the copied file is still an RGBA PNG.
    with Image.open(paths.work / wm["src"]) as im:
        assert im.format == "PNG" and im.mode == "RGBA"


def test_watermark_can_be_disabled(cfg) -> None:
    _write_logo(logo_path(cfg))
    cfg.set("branding.watermark_enabled", False)
    assert resolve_watermark(_paths(cfg), cfg) == (None, None)


def test_invalid_logo_is_skipped_with_warning(cfg) -> None:
    logo = logo_path(cfg)
    logo.parent.mkdir(parents=True, exist_ok=True)
    logo.write_bytes(b"not an image")
    wm, warning = resolve_watermark(_paths(cfg), cfg)
    assert wm is None and warning and "watermark skipped" in warning
    Image.new("RGB", (64, 64), (200, 0, 0)).save(logo, "JPEG")  # wrong format, right name
    assert "PNG" in (validate_logo(logo) or "")
    assert resolve_watermark(_paths(cfg), cfg)[0] is None


def test_unknown_position_falls_back_to_top_right(cfg) -> None:
    _write_logo(logo_path(cfg))
    cfg.set("branding.position", "middle")
    wm, _ = resolve_watermark(_paths(cfg), cfg)
    assert wm is not None and wm["position"] == "top-right"


def test_timeline_carries_watermark_and_assets_are_staged(cfg) -> None:
    from autoeditor.footage_only.timeline import build_timeline

    _write_logo(logo_path(cfg))
    paths = _paths(cfg)
    wm, _ = resolve_watermark(paths, cfg)
    scenes = make_scenes([("a.mp4", 4.0), ("b.mp4", 4.0)])
    script = {"lines": [{"id": 1, "narration": "x", "scene_ids": [1, 2], "overlay_text": "HI", "emphasis_words": []}]}
    timings = make_timings([3.0])
    tl = build_timeline(script, timings, scenes, make_inventory(scenes), paths, cfg, voice_duration=3.0, watermark=wm)
    validate(tl, "timeline")
    assert tl["watermark"] == wm
    assert "branding/logo.png" in collect_asset_paths(tl)
    tl_none = build_timeline(script, timings, scenes, make_inventory(scenes), paths, cfg, voice_duration=3.0, watermark=None)
    assert tl_none["watermark"] is None
    assert "branding/logo.png" not in collect_asset_paths(tl_none)


def test_watermark_schema_rejects_bad_values() -> None:
    from autoeditor.schemas import is_valid

    base = {"version": 1, "fps": 30, "width": 1080, "height": 1920, "duration": 1.0, "voice": "audio/voice.mp3", "lines": []}
    assert is_valid(dict(base, watermark={"src": "branding/logo.png", "position": "top-right", "opacity": 0.8}), "timeline")
    assert not is_valid(dict(base, watermark={"src": "branding/logo.png", "position": "center"}), "timeline")
    assert not is_valid(dict(base, watermark={"src": "branding/logo.png", "position": "top-right", "opacity": 2}), "timeline")


@requires_ffmpeg
def test_end_to_end_with_and_without_logo(inbox: Path, cfg) -> None:
    from autoeditor.footage_only.runner import FootageOnlyOptions, run_footage_only

    opts = FootageOnlyOptions(inbox=inbox, job="no_topic", skip_render=True, skip_vision=True, skip_voice=True)
    results = run_footage_only(cfg, opts)
    assert results[0].status == "skipped_render", results[0].message
    paths = JobPaths.for_job(cfg, "no_topic")
    assert json.loads(paths.timeline_json.read_text(encoding="utf-8"))["watermark"] is None

    # Add a logo and rebuild the timeline stage only.
    _write_logo(logo_path(cfg))
    from autoeditor.pipeline.state import JobState

    state = JobState.load_or_create(paths.state_json, "no_topic", "footage_only")
    state.reset_from("timeline_ready")
    results = run_footage_only(cfg, opts)
    assert results[0].status == "skipped_render", results[0].message
    tl = json.loads(paths.timeline_json.read_text(encoding="utf-8"))
    assert tl["watermark"]["src"] == "branding/logo.png" and (paths.work / "branding" / "logo.png").exists()
    props = json.loads((paths.work / "render_props.json").read_text(encoding="utf-8")) if paths.render_props_json.exists() else None
    assert props is None or props["timeline"]["watermark"]["src"] == "branding/logo.png"
