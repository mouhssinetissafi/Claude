"""CLI parsing, dry run and an end-to-end footage-only run with mock providers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoeditor.cli import build_parser, main
from autoeditor.footage_only.runner import FootageOnlyOptions, run_footage_only
from autoeditor.footage_only.timeline import check_timeline_math
from autoeditor.pipeline.job import JobPaths
from autoeditor.schemas import validate
from tests.conftest import requires_ffmpeg


def test_parser_flags() -> None:
    args = build_parser().parse_args(
        [
            "--footage-only",
            "--inbox",
            "./inbox",
            "--job",
            "x",
            "--skip-vision",
            "--skip-voice",
            "--skip-render",
            "--force-reanalyze",
            "--dry-run",
            "--max-duration",
            "40",
            "--min-scene-duration",
            "1.5",
            "--no-upload",
            "--mock",
        ]
    )
    assert args.footage_only and args.inbox == Path("./inbox") and args.job == "x"
    assert args.skip_vision and args.skip_voice and args.skip_render and args.force_reanalyze and args.dry_run
    assert args.max_duration == 40 and args.min_scene_duration == 1.5 and args.no_upload and args.mock


def test_normal_mode_requires_topic() -> None:
    assert main(["--mock", "--dry-run"]) == 2


def test_upload_flags_conflict() -> None:
    assert main(["--mock", "--footage-only", "--upload", "--no-upload"]) == 2


@requires_ffmpeg
def test_dry_run_writes_nothing(inbox: Path, cfg) -> None:
    results = run_footage_only(cfg, FootageOnlyOptions(inbox=inbox, dry_run=True))
    assert [r.status for r in results] == ["dry_run", "dry_run"]
    assert not (cfg.work_dir / "iphone_air").exists()


@requires_ffmpeg
def test_end_to_end_mock_skip_render_and_resume(inbox: Path, cfg) -> None:
    opts = FootageOnlyOptions(inbox=inbox, job="iphone_air", skip_render=True)
    results = run_footage_only(cfg, opts)
    assert len(results) == 1 and results[0].status == "skipped_render", results[0].message
    paths = JobPaths.for_job(cfg, "iphone_air")

    # Every artifact the renderer needs exists and validates.
    for name, schema in (
        ("scenes.json", "scenes"),
        ("script.json", "script"),
        ("voice_timing.json", "voice_timing"),
        ("captions.json", "captions"),
        ("timeline.json", "timeline"),
    ):
        doc = json.loads((paths.work / name).read_text(encoding="utf-8"))
        validate(doc, schema)
    timeline = json.loads(paths.timeline_json.read_text(encoding="utf-8"))
    check_timeline_math(timeline)
    assert paths.voice_audio.exists()
    assert (paths.work / "inventory.json").exists()
    assert paths.metadata_json.exists() and paths.credits_txt.exists() and paths.thumbnail_jpg.exists()
    metadata = json.loads(paths.metadata_json.read_text(encoding="utf-8"))
    validate(metadata, "metadata")
    assert "#shorts" in metadata["hashtags"]
    credits = paths.credits_txt.read_text(encoding="utf-8")
    assert "Pexels License" in credits and "LICENSE_UNKNOWN" in credits
    script = json.loads(paths.script_json.read_text(encoding="utf-8"))
    assert script["topic"] == "why the iPhone Air is so thin"
    assert script["facts_to_verify"]  # topic asks for facts the footage cannot prove

    state = json.loads(paths.state_json.read_text(encoding="utf-8"))
    assert set(state["stages"]) >= {"discovered", "normalized", "analyzed", "scripted", "voiced", "captioned", "timeline_ready"}
    assert "rendered" not in state["stages"]

    # Vision cache populated; a second run hits the cache and skips completed stages.
    cache_files = list((paths.cache / "vision").glob("*.json"))
    assert cache_files
    mtimes = {p: p.stat().st_mtime_ns for p in (paths.scenes_json, paths.script_json, paths.voice_audio, paths.timeline_json)}
    results2 = run_footage_only(cfg, opts)
    assert results2[0].status == "skipped_render"
    assert {p: p.stat().st_mtime_ns for p in mtimes} == mtimes  # nothing recomputed


@requires_ffmpeg
def test_end_to_end_without_topic_and_skip_vision(inbox: Path, cfg) -> None:
    results = run_footage_only(cfg, FootageOnlyOptions(inbox=inbox, job="no_topic", skip_render=True, skip_vision=True, skip_voice=True))
    assert results[0].status == "skipped_render", results[0].message
    paths = JobPaths.for_job(cfg, "no_topic")
    script = json.loads(paths.script_json.read_text(encoding="utf-8"))
    assert script["topic"] is None
    state = json.loads(paths.state_json.read_text(encoding="utf-8"))
    assert any("placeholder" in w["message"] for w in state["warnings"])


@requires_ffmpeg
def test_cli_main_mock_run(inbox: Path, cfg, monkeypatch) -> None:
    # Route the CLI's config root into the temp dir.
    import autoeditor.cli as cli_mod

    original = cli_mod.load_config

    def patched(user_config, overrides, *, mock=None):  # type: ignore[no-untyped-def]
        c = original(user_config, overrides, root=cfg.root, mock=mock)
        for key in (
            "video.width",
            "video.height",
            "video.crf",
            "video.preset",
            "media.min_width",
            "media.min_height",
            "media.frame_width",
            "audio.music_enabled",
            "audio.sfx_enabled",
        ):
            c.set(key, cfg.get(key))
        return c

    monkeypatch.setattr(cli_mod, "load_config", patched)
    assert main(["--footage-only", "--inbox", str(inbox), "--job", "no_topic", "--mock", "--skip-render", "--skip-vision", "--skip-voice"]) == 0


@pytest.mark.parametrize("argv", [["--footage-only", "--inbox", "/definitely/missing", "--mock"]])
def test_missing_inbox_exit_code(argv: list[str]) -> None:
    assert main(argv) == 2
