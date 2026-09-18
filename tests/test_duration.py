"""45-second minimum: policy math, script expansion, footage sufficiency, QC duration."""

from __future__ import annotations

import json
from pathlib import Path

from autoeditor.config import load_config
from autoeditor.footage_only.shot_plan import estimate_seconds, expand_shot_plan, generate_shot_plan
from autoeditor.media.ffprobe import MediaInfo
from autoeditor.pipeline.duration import DurationPolicy, expansion_instruction, expansion_target, footage_sufficient, narration_shortfall, policy_from_config
from autoeditor.pipeline.job import JobPaths
from autoeditor.pipeline.qc import evaluate_measurements
from autoeditor.providers.mock import MockLLM
from tests.conftest import make_clip, requires_ffmpeg
from tests.test_shot_plan import make_inventory


def test_default_policy_is_45_50_60_70() -> None:
    policy = policy_from_config(load_config(mock=True))
    assert (policy.min_final_seconds, policy.target_min_seconds, policy.target_max_seconds, policy.hard_max_seconds) == (45, 50, 60, 70)
    assert policy.target_seconds == 55


def test_shortfall_and_expansion_math() -> None:
    policy = DurationPolicy()
    assert narration_shortfall(50, policy) == 0
    assert narration_shortfall(44.0, policy) == 0  # within tolerance
    assert narration_shortfall(30, policy) == 15
    assert expansion_target(30, policy) >= 15 + policy.tolerance_seconds
    assert expansion_target(30, policy) == 25  # aims at the middle of the preferred range
    text = expansion_instruction(30, 25, policy)
    assert "45" in text and "filler" in text and "COMPLETE" in text


def test_footage_sufficiency() -> None:
    policy = DurationPolicy()
    ok, why = footage_sufficient(60, policy)
    assert ok and "covers" in why
    bad, why = footage_sufficient(24, policy)
    assert not bad and "insufficient footage" in why and "45" in why
    relaxed = DurationPolicy(min_footage_coverage=0.5)
    assert footage_sufficient(24, relaxed)[0]


def test_mock_writer_reaches_target_and_expands(cfg) -> None:
    cfg.set("script.min_final_seconds", 45)
    cfg.set("script.target_min_seconds", 50)
    cfg.set("script.target_max_seconds", 60)
    cfg.set("script.hard_max_seconds", 70)
    inv = make_inventory(n=12)
    inv["total_usable_seconds"] = 70.0
    paths = JobPaths.for_job(cfg, "dur")
    paths.ensure()
    script = generate_shot_plan(inv, "why the iPhone Air is so thin", MockLLM(), cfg, paths)
    wps = float(cfg.get("script.words_per_second"))
    est = estimate_seconds(script["lines"], wps)
    assert 45 <= est <= 70, est
    assert script["expansions"] == 0
    first, last = script["lines"][0]["narration"], script["lines"][-1]["narration"]
    longer = expand_shot_plan(script, inv, MockLLM(), cfg, paths, current_seconds=est, add_seconds=8)
    assert longer is not None and longer["expansions"] == 1
    assert estimate_seconds(longer["lines"], wps) > est + 5
    assert longer["lines"][0]["narration"] == first and longer["lines"][-1]["narration"] == last
    assert len({ln["narration"] for ln in longer["lines"]}) == len(longer["lines"])  # no repeated lines


def test_qc_duration_uses_45_second_minimum() -> None:
    cfg = load_config(mock=True)
    info = MediaInfo(
        filename="f",
        path="f",
        duration=40.0,
        width=1080,
        height=1920,
        fps=30,
        codec="h264",
        aspect_ratio=0.56,
        has_audio=True,
        has_video=True,
        audio_codec="aac",
    )
    short = evaluate_measurements(
        info=info, expected_duration=40.0, black=[], silence=[], volume={"max_volume": -3}, captions_end=39, narration_end=39.5, cfg=cfg
    )
    assert not short["checks"]["duration"]["passed"]
    info.duration = 55.0
    good = evaluate_measurements(
        info=info, expected_duration=55.0, black=[], silence=[], volume={"max_volume": -3}, captions_end=54, narration_end=54.5, cfg=cfg
    )
    assert good["checks"]["duration"]["passed"]


@requires_ffmpeg
def test_insufficient_footage_goes_to_review_without_scripting(inbox: Path, cfg) -> None:
    from autoeditor.footage_only.runner import FootageOnlyOptions, run_footage_only

    cfg.set("script.min_final_seconds", 45)
    cfg.set("script.min_footage_coverage", 1.0)
    results = run_footage_only(cfg, FootageOnlyOptions(inbox=inbox, job="iphone_air", skip_render=True, skip_vision=True, skip_voice=True))
    assert results[0].status == "needs_review"
    assert "insufficient footage" in results[0].message
    paths = JobPaths.for_job(cfg, "iphone_air")
    assert not paths.script_json.exists()  # no writer call, no TTS, no render
    review = (paths.output / "REVIEW.md").read_text(encoding="utf-8")
    assert "STOPPED" in review and "insufficient footage" in review
    state = json.loads(paths.state_json.read_text(encoding="utf-8"))
    assert state["status"] == "needs_review"


@requires_ffmpeg
def test_enough_footage_yields_at_least_45_seconds(tmp_path: Path, cfg) -> None:
    from autoeditor.footage_only.runner import FootageOnlyOptions, run_footage_only
    from autoeditor.footage_only.timeline import check_timeline_math

    root = tmp_path / "inbox45"
    job = root / "long_job"
    for i in range(3):
        make_clip(job / f"clip{i}.mp4", seconds=20.0, size="320x240", scenes=4, audio=False)
    (job / "topic.txt").write_text("why the iPhone Air is so thin", encoding="utf-8")
    cfg.set("script.min_final_seconds", 45)
    cfg.set("script.target_min_seconds", 50)
    cfg.set("script.target_max_seconds", 60)
    cfg.set("script.hard_max_seconds", 70)
    cfg.set("script.min_footage_coverage", 1.0)
    cfg.set("qc.min_duration_seconds", 45)
    results = run_footage_only(cfg, FootageOnlyOptions(inbox=root, skip_render=True, skip_vision=True, skip_voice=True))
    assert results[0].status == "skipped_render", results[0].message
    paths = JobPaths.for_job(cfg, "long_job")
    tl = json.loads(paths.timeline_json.read_text(encoding="utf-8"))
    check_timeline_math(tl)  # 1x playback only, never slowed
    assert tl["duration"] >= 45.0, tl["duration"]
    assert tl["duration"] <= 75.0
    timing = json.loads(paths.voice_timing_json.read_text(encoding="utf-8"))
    assert timing[-1]["end"] >= 44.0
    captions = json.loads(paths.captions_json.read_text(encoding="utf-8"))
    assert captions["duration"] >= 44.0
    review = (paths.output / "REVIEW.md").read_text(encoding="utf-8")
    assert "OK" in review and "minimum 45s" in review
