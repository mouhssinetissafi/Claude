"""High-performance style reference system: tiers, weighting, pruning, profile, influence log."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from autoeditor.config import load_config
from autoeditor.style import cli as style_cli
from autoeditor.style.analysis import compare_top_vs_baseline, load_annotations, title_features
from autoeditor.style.models import TIER_HIGH, TIER_LOW, TIER_MEDIUM, TIER_UNVERIFIED, ChannelStats, VideoStats
from autoeditor.style.profile import apply_to_config, build_profile, influence_lines, load_or_build_profile, prompt_block
from autoeditor.style.references import channels_from_config, classify, compute_signals, evaluate_channels, load_references, load_stats, save_stats
from autoeditor.style.youtube_data import ManualImportSource, ReferenceFetchError, parse_iso8601_duration
from tests.conftest import requires_ffmpeg

NOW = datetime(2026, 9, 18, tzinfo=UTC)


def _video(i: int, views: int, *, days_ago: float = 5, seconds: float = 50, likes: int | None = None, title: str | None = None) -> VideoStats:
    return VideoStats(
        video_id=f"v{i}",
        title=title or f"Short number {i}",
        published_at=(NOW - timedelta(days=days_ago + i * 0.5)).isoformat(),
        duration_seconds=seconds,
        views=views,
        likes=likes if likes is not None else views // 40,
        comments=views // 400,
        is_short=seconds <= 180,
    )


def _stats(handle: str, videos: list[VideoStats], *, fetched_days_ago: float = 0) -> ChannelStats:
    return ChannelStats(handle=handle, channel_id="UC" + handle, fetched_at=(NOW - timedelta(days=fetched_days_ago)).isoformat(), videos=videos, source="mock")


def _perf() -> dict:
    return load_references(load_config(mock=True))["performance"]


# --------------------------------------------------------------------------- #
def test_registry_loads_with_roles_and_trust() -> None:
    refs = load_references(load_config(mock=True))
    channels = channels_from_config(refs)
    handles = {c.handle for c in channels}
    assert {"Mrwhosetheboss", "mkbhd", "KyleKrueger", "TechnicalGuruji", "OmarDizer"} <= handles
    assert sum(1 for c in channels if c.trust == "user_verified_strong") == 5
    assert {c.handle for c in channels if c.role == "research"} == {"TechAltar", "ColdFusion"}
    assert len(channels) == 23
    assert refs["house_style"]["principles"]


def test_duration_parsing() -> None:
    assert parse_iso8601_duration("PT58S") == 58
    assert parse_iso8601_duration("PT1M2S") == 62
    assert parse_iso8601_duration("PT2H3M") == 7380
    assert parse_iso8601_duration("P1DT1S") == 86401
    with pytest.raises(ValueError):
        parse_iso8601_duration("nonsense")


def test_tiers_from_signals_not_subscribers() -> None:
    perf = _perf()
    strong = _stats("big", [_video(i, 1_500_000 if i < 4 else 300_000) for i in range(12)])
    sig = compute_signals(strong, perf, now=NOW)
    tier, met = classify(sig, perf)
    assert tier == TIER_HIGH and any("over 1,000,000" in m for m in met)

    medium = _stats("mid", [_video(i, 200_000) for i in range(12)])
    assert classify(compute_signals(medium, perf, now=NOW), perf)[0] == TIER_MEDIUM

    weak = _stats("weak", [_video(i, 8_000) for i in range(12)])
    assert classify(compute_signals(weak, perf, now=NOW), perf)[0] == TIER_LOW

    # A smaller creator with repeated outliers well above its own baseline is valuable.
    spiky = _stats("spiky", [_video(i, 900_000 if i in (1, 5) else 60_000) for i in range(12)])
    tier, met = classify(compute_signals(spiky, perf, now=NOW), perf)
    assert tier == TIER_HIGH and any("x the creator's median" in m for m in met)

    # Tiny sample = insufficient data, never guessed.
    thin = _stats("thin", [_video(i, 5_000_000) for i in range(3)])
    assert classify(compute_signals(thin, perf, now=NOW), perf)[0] == TIER_UNVERIFIED


def test_signals_include_28d_lower_bound_velocity_and_long_form_ratio() -> None:
    perf = _perf()
    videos = [_video(i, 400_000, days_ago=3) for i in range(6)] + [_video(20 + i, 100_000, days_ago=60) for i in range(4)]
    videos.append(
        VideoStats(video_id="long1", title="Review", published_at=(NOW - timedelta(days=10)).isoformat(), duration_seconds=900, views=50_000, is_short=False)
    )
    sig = compute_signals(_stats("c", videos), perf, now=NOW)
    assert sig.views_28d_lower_bound == 6 * 400_000
    assert sig.shorts_in_sample == 10 and sig.long_form_in_sample == 1
    assert sig.median_views_per_hour > 0
    assert sig.shorts_vs_long_form_ratio and sig.shorts_vs_long_form_ratio > 1
    assert sig.median_engagement_rate is not None


def test_evaluation_weights_provisional_stale_research_and_pruning(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, mock=True)
    refs = load_references(cfg)
    perf = dict(refs["performance"])
    perf["max_active_references"] = 2
    channels = channels_from_config(refs)
    stats = {
        "mkbhd": _stats("mkbhd", [_video(i, 2_000_000) for i in range(12)]),
        "beebomco": _stats("beebomco", [_video(i, 250_000) for i in range(12)]),
        "ziadpro": _stats("ziadpro", [_video(i, 3_000) for i in range(12)]),
        "techaltar": _stats("TechAltar", [_video(i, 3_000_000) for i in range(12)]),
        "coldfusion": _stats("ColdFusion", [_video(i, 200_000) for i in range(12)]),
        "kylekrueger": _stats("KyleKrueger", [_video(i, 2_000_000) for i in range(12)], fetched_days_ago=90),
    }
    evals = {e.handle: e for e in evaluate_channels(channels, stats, perf, now=NOW)}
    assert evals["mkbhd"].tier == TIER_HIGH and evals["mkbhd"].weight == 1.0 and evals["mkbhd"].active
    assert evals["beebomco"].tier == TIER_MEDIUM and evals["beebomco"].weight == 0.5
    assert evals["ziadpro"].tier == TIER_LOW and evals["ziadpro"].weight == 0.0 and not evals["ziadpro"].active
    # Research channels shape style only when their own Shorts tier is high.
    assert evals["TechAltar"].tier == TIER_HIGH and evals["TechAltar"].weight == 1.0
    assert evals["ColdFusion"].weight == 0.0 and "research" in evals["ColdFusion"].reason
    # Stale stats are excluded until refreshed, even for a user-verified channel.
    assert evals["KyleKrueger"].tier == TIER_UNVERIFIED and evals["KyleKrueger"].weight == 0.0 and "days old" in evals["KyleKrueger"].reason
    # Once any statistics exist, an unfetched channel is simply unverified, whoever supplied it.
    assert evals["Mrwhosetheboss"].tier == TIER_UNVERIFIED and evals["Mrwhosetheboss"].weight == 0.0
    assert evals["atolethros"].weight == 0.0
    # Before the first refresh, user-verified channels carry only a provisional weight.
    fresh = {e.handle: e for e in evaluate_channels(channels, {}, perf, now=NOW)}
    assert fresh["Mrwhosetheboss"].weight == perf["provisional_weight_user_verified"] and fresh["beebomco"].weight == 0.0
    assert fresh["TechAltar"].weight == 0.0  # research role never gets provisional style weight
    # Pruning keeps the top N by weight.
    active = [e for e in evals.values() if e.active]
    assert len(active) == 2 and {e.handle for e in active} == {"mkbhd", "TechAltar"}
    assert any("pruned" in e.reason for e in evals.values() if e.weight > 0 and not e.active)


def test_video_level_findings_compare_outperformers_with_baseline() -> None:
    perf = _perf()
    videos = [_video(i, 1_200_000, seconds=38, title=f"3 things about phone {i}") for i in range(3)]
    videos += [_video(10 + i, 150_000, seconds=58, title=f"Thoughts on the phone, part {'abcdefg'[i]}") for i in range(7)]
    st = _stats("c", videos)
    compute_signals(st, perf, now=NOW)
    findings = compare_top_vs_baseline(st.shorts, 2.0)
    assert any("run a median 38s vs 58s" in f for f in findings)
    assert any("number in the title" in f for f in findings)
    assert compare_top_vs_baseline(st.shorts[:4], 2.0) == []  # too small to conclude anything
    assert title_features("iPhone vs Pixel: 5 differences?") == {
        "question": True,
        "has_number": True,
        "comparison": True,
        "caps_ratio": 0.08,  # two capital "P"s among 24 letters
        "word_count": 5,
        "exclamation": False,
    }


def test_profile_without_stats_is_baseline_only_and_says_so(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, mock=True)
    profile = build_profile(cfg, now=NOW)
    assert profile["source"] == "editorial_baseline_only"
    # Before any refresh the five user-vouched channels are active only provisionally (no signals).
    assert profile["principles"] and profile["active_references"]
    assert all(e["signals"] is None and e["tier"] == TIER_UNVERIFIED for e in profile["active_references"])
    assert profile["guidance"]["cadence"]["source"] == "editorial_baseline"
    block = prompt_block(profile)
    assert "No verified reference statistics" in block and "never copy" in block
    assert "Mrwhosetheboss" not in block  # unverified channels are never presented as evidence
    assert apply_to_config(profile, cfg) == []  # baseline never rewrites config
    lines = influence_lines(profile)
    assert any("no measured references" in ln for ln in lines)
    assert any("Mrwhosetheboss" in ln and "provisional" in ln for ln in lines)
    assert any("excluded beebomco" in ln for ln in lines)


def test_profile_with_stats_and_annotations_logs_influences(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, mock=True)
    stats = {
        "mkbhd": _stats(
            "mkbhd", [_video(i, 3_000_000 if i < 3 else 400_000, seconds=45 if i < 3 else 60, title=f"{i} reasons" if i < 3 else "Thoughts") for i in range(12)]
        ),
        "ziadpro": _stats("ziadpro", [_video(i, 2_000) for i in range(12)]),
    }
    save_stats(cfg, stats)
    assert load_stats(cfg).keys() == {"mkbhd", "ziadpro"}
    ann_dir = tmp_path / "references" / "annotations"
    ann_dir.mkdir(parents=True)
    (ann_dir / "mkbhd.json").write_text(
        json.dumps(
            {
                "handle": "mkbhd",
                "observed_by": "tester",
                "observed_at": "2026-09-18",
                "videos": [
                    {
                        "video_id": "v0",
                        "hook_type": "claim",
                        "first_visual_seconds": 0.4,
                        "avg_cut_seconds": 1.6,
                        "caption_style": "word",
                        "emphasis": "selective",
                        "notes": "opens on the product",
                    },
                    {"video_id": "v1", "hook_type": "claim", "avg_cut_seconds": 2.0, "caption_style": "word"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (ann_dir / "_template.json").write_text("{}", encoding="utf-8")
    (ann_dir / "broken.json").write_text("{not json", encoding="utf-8")
    assert set(load_annotations(ann_dir)) == {"mkbhd"}

    profile = build_profile(cfg, now=NOW)
    assert profile["source"] == "references+annotations+baseline"
    assert [e["handle"] for e in profile["active_references"]] == ["mkbhd"]
    assert any(ex["handle"] == "ziadpro" and ex["tier"] == TIER_LOW for ex in profile["excluded_references"])
    cad = profile["guidance"]["cadence"]
    assert cad["source"].startswith("weighted mean cut length") and 1.0 <= cad["target_segment_seconds"] <= 3.5
    assert profile["guidance"]["captions"]["max_words_per_caption"] == 3
    assert any("'claim' hook" in n for n in profile["guidance"]["hook"]["notes"])
    assert any(i.get("video_id") == "v0" and "opens on the product" in i["why"] for i in profile["influences"])
    assert any("run a median" in n for n in profile["evidence_notes"])

    changes = apply_to_config(profile, cfg)
    assert any("timeline.max_segment_seconds" in c for c in changes) and cfg.get("captions.max_words_per_caption") == 3
    block = prompt_block(profile)
    assert "mkbhd (weight 1.0)" in block and "Hook evidence" in block
    assert any("mkbhd video v0" in ln for ln in influence_lines(profile))


def test_manual_import_and_cli_actions(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, mock=True)
    export = {
        "channels": [
            {"handle": "mkbhd", "channel_id": "UC1", "fetched_at": NOW.isoformat(), "videos": [_video(i, 1_500_000).to_dict() for i in range(10)]},
        ]
    }
    src = ManualImportSource(export)
    stats = src.fetch_channel("MKBHD", sample_size=20, shorts_max_seconds=180)
    assert stats.source == "manual_import" and len(stats.shorts) == 10
    with pytest.raises(ReferenceFetchError):
        src.fetch_channel("nobody", sample_size=20, shorts_max_seconds=180)

    path = tmp_path / "stats.json"
    path.write_text(json.dumps(export), encoding="utf-8")
    assert style_cli.run("import", cfg, import_path=path) == 0
    saved = load_stats(cfg)
    assert saved["mkbhd"].source == "manual_import"
    assert saved["beebomco"].error and "no manual statistics" in saved["beebomco"].error
    assert (tmp_path / "cache" / "references" / "style_profile.json").exists()
    assert style_cli.run("report", cfg) == 0
    assert style_cli.run("profile", cfg) == 0
    assert style_cli.run("import", cfg, import_path=tmp_path / "missing.json") == 2
    profile = load_or_build_profile(cfg)
    assert profile and [e["handle"] for e in profile["active_references"]] == ["mkbhd"]


def test_refresh_without_api_key_fails_cleanly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    cfg = load_config(root=tmp_path, mock=True)
    assert style_cli.run("refresh", cfg) == 3


def test_registry_is_configurable(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, mock=True)
    custom = tmp_path / "refs.yaml"
    custom.write_text(
        yaml.safe_dump(
            {
                "performance": {
                    "sample_size": 10,
                    "min_shorts_sample": 3,
                    "strong": {"median_views": 100},
                    "medium": {},
                    "weights": {"high": 1.0, "medium": 0.5, "low": 0.0},
                    "max_active_references": 1,
                },
                "channels": [{"handle": "newstar", "role": "style", "trust": "unverified"}],
                "house_style": {"principles": ["Only one rule"], "defaults": {}},
            }
        ),
        encoding="utf-8",
    )
    cfg.set("style.references_config", str(custom))
    save_stats(cfg, {"newstar": _stats("newstar", [_video(i, 500) for i in range(5)])})
    profile = build_profile(cfg, now=NOW)
    assert profile["principles"] == ["Only one rule"]
    assert [e["handle"] for e in profile["active_references"]] == ["newstar"]


@requires_ffmpeg
def test_pipeline_records_style_profile_per_job(inbox: Path, cfg) -> None:
    from autoeditor.footage_only.runner import FootageOnlyOptions, run_footage_only
    from autoeditor.pipeline.job import JobPaths

    results = run_footage_only(cfg, FootageOnlyOptions(inbox=inbox, job="iphone_air", skip_render=True, skip_vision=True, skip_voice=True))
    assert results[0].status == "skipped_render", results[0].message
    paths = JobPaths.for_job(cfg, "iphone_air")
    job_profile = json.loads((paths.work / "style_profile.json").read_text(encoding="utf-8"))
    assert job_profile["source"] == "editorial_baseline_only"
    review = (paths.output / "REVIEW.md").read_text(encoding="utf-8")
    assert "Style influences" in review and "no measured references" in review
    assert (cfg.root / "cache" / "references" / "style_profile.json").exists()
