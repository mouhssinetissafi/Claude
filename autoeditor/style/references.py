"""Reference registry: config loading, statistics persistence, performance tiers, pruning.

Rules implemented here (all thresholds live in config/references.yaml):
* subscriber counts are never a signal
* a channel is "high" if ANY strong signal is met, "medium" if ANY medium signal,
  otherwise "low" and excluded
* insufficient or stale statistics = "unverified" = excluded (a user-verified
  channel keeps a small provisional weight only until stats exist)
* research-role channels never shape editing style unless their own Shorts tier is high
* at most ``max_active_references`` channels influence the profile
"""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from autoeditor.config import REPO_ROOT, Config
from autoeditor.logging_utils import get_logger
from autoeditor.style.models import (
    TIER_HIGH,
    TIER_LOW,
    TIER_MEDIUM,
    TIER_UNVERIFIED,
    ChannelEvaluation,
    ChannelStats,
    PerformanceSignals,
    ReferenceChannel,
    VideoStats,
)

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Config + persistence
# --------------------------------------------------------------------------- #
def _shipped_path(cfg: Config, key: str, default: str) -> Path:
    """Resolve a file that ships with the project: config root first, then the repo root."""
    raw = Path(str(cfg.get(key, default)))
    if raw.is_absolute():
        return raw
    local = cfg.root / raw
    if local.exists():
        return local
    fallback = REPO_ROOT / raw
    return fallback if fallback.exists() else local


def references_config_path(cfg: Config) -> Path:
    return _shipped_path(cfg, "style.references_config", "config/references.yaml")


def stats_path(cfg: Config) -> Path:
    raw = Path(str(cfg.get("style.stats_path", "cache/references/stats.json")))
    return raw if raw.is_absolute() else cfg.root / raw


def profile_path(cfg: Config) -> Path:
    raw = Path(str(cfg.get("style.profile_path", "cache/references/style_profile.json")))
    return raw if raw.is_absolute() else cfg.root / raw


def annotations_dir(cfg: Config) -> Path:
    return _shipped_path(cfg, "style.annotations_dir", "references/annotations")


def load_references(cfg: Config) -> dict[str, Any]:
    path = references_config_path(cfg)
    if not path.exists():
        raise FileNotFoundError(f"reference registry not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or "channels" not in data:
        raise ValueError(f"{path} must contain a 'channels' list")
    return data


def channels_from_config(data: dict[str, Any]) -> list[ReferenceChannel]:
    out: list[ReferenceChannel] = []
    for raw in data.get("channels", []):
        out.append(
            ReferenceChannel(
                handle=str(raw["handle"]).lstrip("@"),
                url=str(raw.get("url", "")),
                role=str(raw.get("role", "style")).lower(),
                trust=str(raw.get("trust", "unverified")).lower(),
                notes=str(raw.get("notes", "")),
            )
        )
    return out


def load_stats(cfg: Config) -> dict[str, ChannelStats]:
    path = stats_path(cfg)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("reference stats %s unreadable; ignoring", path)
        return {}
    out: dict[str, ChannelStats] = {}
    for raw in data.get("channels", []):
        try:
            stats = ChannelStats.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            continue
        out[stats.handle.lower()] = stats
    return out


def save_stats(cfg: Config, stats: dict[str, ChannelStats]) -> Path:
    path = stats_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "saved_at": datetime.now(UTC).isoformat(timespec="seconds"), "channels": [s.to_dict() for s in stats.values()]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


# --------------------------------------------------------------------------- #
# Derived metrics
# --------------------------------------------------------------------------- #
def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def enrich_videos(videos: list[VideoStats], *, now: datetime | None = None) -> None:
    """Fill hours_since_publish, views_per_hour, engagement_rate and outlier_score (Shorts only)."""
    now = now or datetime.now(UTC)
    shorts = [v for v in videos if v.is_short]
    median = statistics.median([v.views for v in shorts]) if shorts else 0.0
    for v in videos:
        ts = _parse_ts(v.published_at)
        if ts is not None:
            hours = max(1.0, (now - ts).total_seconds() / 3600.0)
            v.hours_since_publish = round(hours, 1)
            v.views_per_hour = round(v.views / hours, 2)
        if v.views > 0 and v.likes is not None:
            v.engagement_rate = round((v.likes + (v.comments or 0)) / v.views, 4)
        v.outlier_score = round(v.views / median, 2) if (v.is_short and median > 0) else None


def compute_signals(stats: ChannelStats, perf: dict[str, Any], *, now: datetime | None = None) -> PerformanceSignals:
    now = now or datetime.now(UTC)
    enrich_videos(stats.videos, now=now)
    shorts = stats.shorts
    long_form = stats.long_form
    sig = PerformanceSignals(shorts_in_sample=len(shorts), long_form_in_sample=len(long_form))
    if not shorts:
        return sig
    views = [v.views for v in shorts]
    sig.median_views = float(statistics.median(views))
    sig.max_views = max(views)
    window = float(perf.get("recent_days", 28)) * 24.0
    sig.views_28d_lower_bound = sum(v.views for v in shorts if v.hours_since_publish is not None and v.hours_since_publish <= window)
    strong = perf.get("strong", {})
    sig.viral_count = sum(1 for v in shorts if v.views >= int(strong.get("viral_video_views", 1_000_000)))
    ratio = float(strong.get("outlier_ratio", 3.0))
    floor = float(strong.get("outlier_median_floor", 20_000))
    sig.outlier_count = sum(1 for v in shorts if v.outlier_score is not None and v.outlier_score >= ratio) if sig.median_views >= floor else 0
    vph = [v.views_per_hour for v in shorts if v.views_per_hour is not None]
    sig.median_views_per_hour = float(statistics.median(vph)) if vph else 0.0
    eng = [v.engagement_rate for v in shorts if v.engagement_rate is not None]
    sig.median_engagement_rate = round(float(statistics.median(eng)), 4) if eng else None
    if long_form:
        lf_median = statistics.median([v.views for v in long_form])
        sig.shorts_vs_long_form_ratio = round(sig.median_views / lf_median, 2) if lf_median > 0 else None
    return sig


def classify(sig: PerformanceSignals, perf: dict[str, Any]) -> tuple[str, list[str]]:
    """Return (tier, signals_met). ANY strong signal -> high; ANY medium signal -> medium."""
    if sig.shorts_in_sample < int(perf.get("min_shorts_sample", 5)):
        return TIER_UNVERIFIED, [f"only {sig.shorts_in_sample} recent Shorts in sample"]
    strong = perf.get("strong", {})
    medium = perf.get("medium", {})
    met: list[str] = []
    if sig.views_28d_lower_bound >= int(strong.get("views_28d", 5_000_000)):
        met.append(f"{sig.views_28d_lower_bound:,} views on Shorts published in the last {perf.get('recent_days', 28)} days")
    if sig.viral_count >= int(strong.get("viral_videos_min", 3)):
        met.append(f"{sig.viral_count} recent Shorts over {int(strong.get('viral_video_views', 1_000_000)):,} views")
    if sig.median_views >= float(strong.get("median_views", 500_000)):
        met.append(f"median {sig.median_views:,.0f} views across the latest {sig.shorts_in_sample} Shorts")
    if sig.outlier_count >= int(strong.get("outlier_min_count", 2)):
        met.append(f"{sig.outlier_count} Shorts at >= {strong.get('outlier_ratio', 3.0)}x the creator's median")
    if met:
        return TIER_HIGH, met
    if sig.views_28d_lower_bound >= int(medium.get("views_28d", 1_000_000)):
        met.append(f"{sig.views_28d_lower_bound:,} recent-window views")
    if sig.viral_count >= int(medium.get("viral_videos_min", 1)):
        met.append(f"{sig.viral_count} recent Short(s) over {int(strong.get('viral_video_views', 1_000_000)):,} views")
    if sig.median_views >= float(medium.get("median_views", 150_000)):
        met.append(f"median {sig.median_views:,.0f} views")
    if sig.outlier_count >= int(medium.get("outlier_min_count", 1)):
        met.append(f"{sig.outlier_count} outlier Short(s)")
    if met:
        return TIER_MEDIUM, met
    return TIER_LOW, [f"median {sig.median_views:,.0f} views, max {sig.max_views:,}, no viral or outlier Shorts"]


# --------------------------------------------------------------------------- #
# Evaluation + pruning
# --------------------------------------------------------------------------- #
def evaluate_channels(
    channels: list[ReferenceChannel],
    stats: dict[str, ChannelStats],
    perf: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[ChannelEvaluation]:
    """Tier and weight every channel; ``active`` marks the pruned set that shapes the profile."""
    now = now or datetime.now(UTC)
    weights = perf.get("weights", {})
    stale_after = float(perf.get("stale_after_days", 45))
    provisional = float(perf.get("provisional_weight_user_verified", 0.6))
    # The operator's own assessment counts only until the first refresh; after that the numbers decide.
    provisional_allowed = not stats
    evaluations: list[ChannelEvaluation] = []
    for ch in channels:
        st = stats.get(ch.key)
        if st is None or st.error:
            reason = st.error if (st and st.error) else "no statistics fetched yet (run --references refresh)"
            weight = provisional if (provisional_allowed and ch.trust == "user_verified_strong" and ch.role == "style") else 0.0
            tier = TIER_UNVERIFIED
            if weight > 0:
                reason += f"; provisional weight {provisional} because the operator marked it user_verified_strong (counts only for annotation weighting until statistics exist)"
            evaluations.append(ChannelEvaluation(handle=ch.handle, role=ch.role, trust=ch.trust, tier=tier, weight=weight, reason=reason))
            continue
        age = None
        fetched = _parse_ts(st.fetched_at)
        if fetched is not None:
            age = round((now - fetched).total_seconds() / 86400.0, 1)
        if age is not None and age > stale_after:
            evaluations.append(
                ChannelEvaluation(
                    handle=ch.handle,
                    role=ch.role,
                    trust=ch.trust,
                    tier=TIER_UNVERIFIED,
                    weight=0.0,
                    reason=f"statistics are {age:.0f} days old (limit {stale_after:.0f}); refresh before use",
                    stats_age_days=age,
                )
            )
            continue
        sig = compute_signals(st, perf, now=now)
        tier, met = classify(sig, perf)
        sig.signals_met = met
        weight = float(weights.get(tier, 0.0))
        reason = "; ".join(met)
        if ch.role == "research" and tier != TIER_HIGH:
            weight = 0.0
            reason = f"research reference (narrative/context only); Shorts tier {tier} does not justify shaping the editing style"
        top = top_videos(st, perf)
        findings = video_level_findings(st, perf)
        evaluations.append(
            ChannelEvaluation(
                handle=ch.handle,
                role=ch.role,
                trust=ch.trust,
                tier=tier,
                weight=weight,
                reason=reason,
                signals=sig,
                stats_age_days=age,
                top_videos=top,
                video_level_findings=findings,
            )
        )
    # Pruning: keep the strongest few weighted channels.
    ranked = sorted((e for e in evaluations if e.weight > 0), key=lambda e: (-e.weight, -(e.signals.median_views if e.signals else 0), e.handle.lower()))
    limit = int(perf.get("max_active_references", 6))
    for i, ev in enumerate(ranked):
        ev.active = i < limit
        if not ev.active:
            ev.reason += f"; pruned (only the top {limit} references are active)"
    return evaluations


def top_videos(stats: ChannelStats, perf: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    ratio = float(perf.get("video_top_outlier_ratio", 2.0))
    shorts = [v for v in stats.shorts if v.outlier_score is not None]
    tops = sorted((v for v in shorts if v.outlier_score is not None and v.outlier_score >= ratio), key=lambda v: -v.views)[:limit]
    if not tops:
        tops = sorted(shorts, key=lambda v: -v.views)[: min(3, len(shorts))]
    return [
        {
            "video_id": v.video_id,
            "title": v.title,
            "views": v.views,
            "outlier_score": v.outlier_score,
            "duration_seconds": v.duration_seconds,
            "views_per_hour": v.views_per_hour,
            "engagement_rate": v.engagement_rate,
        }
        for v in tops
    ]


def video_level_findings(stats: ChannelStats, perf: dict[str, Any]) -> list[str]:
    """Compare a creator's outperforming Shorts with their baseline using public metadata only."""
    from autoeditor.style.analysis import compare_top_vs_baseline

    return compare_top_vs_baseline(stats.shorts, float(perf.get("video_top_outlier_ratio", 2.0)))
