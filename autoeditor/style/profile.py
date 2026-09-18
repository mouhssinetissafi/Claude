"""Build the weighted house-style profile and expose it to the pipeline.

The profile always starts from the editorial baseline in config/references.yaml.
Active references (tier high/medium, not pruned) add *evidence* with weights;
human annotations of outperforming videos may adjust concrete knobs (cut
cadence, caption grouping) within safe bounds. Every adjustment records which
creators/videos caused it and why. Nothing is invented when data is missing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autoeditor.config import Config
from autoeditor.logging_utils import get_logger
from autoeditor.style.analysis import load_annotations, summarize_annotations
from autoeditor.style.models import TIER_HIGH, ChannelEvaluation, ChannelStats
from autoeditor.style.references import annotations_dir, channels_from_config, evaluate_channels, load_references, load_stats, profile_path

log = get_logger(__name__)

PROFILE_VERSION = 1
CADENCE_BOUNDS = (1.0, 3.5)
CAPTION_WORDS_BOUNDS = (2, 5)


def build_profile(cfg: Config, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    refs = load_references(cfg)
    perf = refs.get("performance", {})
    house = refs.get("house_style", {})
    defaults = dict(house.get("defaults", {}))
    channels = channels_from_config(refs)
    stats = load_stats(cfg)
    evaluations = evaluate_channels(channels, stats, perf, now=now)
    active = [e for e in evaluations if e.active]
    excluded = [e for e in evaluations if not e.active]

    guidance: dict[str, Any] = {
        "cadence": {
            "min_segment_seconds": float(defaults.get("min_segment_seconds", 1.2)),
            "target_segment_seconds": float(defaults.get("target_segment_seconds", 2.2)),
            "max_segment_seconds": float(defaults.get("max_segment_seconds", 4.5)),
            "source": "editorial_baseline",
        },
        "captions": {"max_words_per_caption": int(defaults.get("max_words_per_caption", 4)), "emphasis": "selective", "source": "editorial_baseline"},
        "hook": {"notes": [], "source": "editorial_baseline"},
        "duration": {"notes": [], "source": "duration policy in config/default.yaml (never lowered by references)"},
    }
    influences: list[dict[str, Any]] = []
    evidence_notes: list[str] = []

    # Evidence from measured performance (metadata level).
    for ev in active:
        why = f"tier {ev.tier}, weight {ev.weight:.2f}: {ev.reason}"
        entry = {
            "handle": ev.handle,
            "role": ev.role,
            "weight": ev.weight,
            "tier": ev.tier,
            "why": why,
            "top_videos": ev.top_videos,
            "findings": ev.video_level_findings,
        }
        influences.append(entry)
        for finding in ev.video_level_findings:
            evidence_notes.append(f"{ev.handle} (w={ev.weight:.1f}): {finding}")
            if "median" in finding and "run a median" in finding:
                guidance["duration"]["notes"].append(f"{ev.handle}: {finding}")
            if "title" in finding:
                guidance["hook"]["notes"].append(f"{ev.handle}: {finding}")

    # Evidence from human annotations of outperforming videos (editing level).
    ann = load_annotations(annotations_dir(cfg))
    weights = {e.handle.lower(): e.weight for e in active}
    outliers: dict[str, dict[str, float]] = {}
    for handle, st in stats.items():
        outliers[handle] = {v.video_id: float(v.outlier_score) for v in st.shorts if v.outlier_score is not None}
    summary: dict[str, Any] = summarize_annotations(ann, weights, outliers) if ann else {"annotated_videos": 0}
    if summary.get("annotated_videos"):
        cut = summary.get("avg_cut_seconds")
        if cut is not None:
            target = max(CADENCE_BOUNDS[0], min(CADENCE_BOUNDS[1], float(cut)))
            guidance["cadence"].update(
                {
                    "target_segment_seconds": round(target, 2),
                    "min_segment_seconds": round(max(0.8, target * 0.55), 2),
                    "max_segment_seconds": round(max(target * 2.0, 3.0), 2),
                    "source": f"weighted mean cut length {cut}s across {summary['annotated_videos']} annotated outperforming videos",
                }
            )
        style = summary.get("dominant_caption_style")
        if style == "word":
            guidance["captions"].update(
                {"max_words_per_caption": max(CAPTION_WORDS_BOUNDS[0], 3), "source": "annotations: word-level captions dominate outperformers"}
            )
        elif style == "phrase":
            guidance["captions"].update(
                {"max_words_per_caption": min(CAPTION_WORDS_BOUNDS[1], 5), "source": "annotations: phrase captions dominate outperformers"}
            )
        if summary.get("dominant_emphasis"):
            guidance["captions"]["emphasis"] = summary["dominant_emphasis"]
        if summary.get("dominant_hook_type"):
            guidance["hook"]["notes"].append(f"annotated outperformers most often open with a '{summary['dominant_hook_type']}' hook")
            guidance["hook"]["source"] = "annotations"
        if summary.get("first_visual_seconds") is not None:
            guidance["hook"]["notes"].append(f"first meaningful visual arrives at ~{summary['first_visual_seconds']}s in annotated outperformers")
        for used in summary.get("videos_used", []):
            influences.append(
                {
                    "handle": used["handle"],
                    "video_id": used["video_id"],
                    "weight": used["weight"],
                    "why": "human-annotated editing traits of an outperforming Short" + (f": {used['notes']}" if used["notes"] else ""),
                }
            )

    measured = [e for e in active if e.signals is not None]
    annotated = bool(summary.get("annotated_videos"))
    if measured:
        source = "references+annotations+baseline" if annotated else "references+baseline"
    elif annotated:
        source = "annotations+baseline"
    else:
        source = "editorial_baseline_only"
    profile = {
        "version": PROFILE_VERSION,
        "built_at": now.isoformat(timespec="seconds"),
        "source": source,
        "principles": list(house.get("principles", [])),
        "guidance": guidance,
        "evidence_notes": evidence_notes,
        "influences": influences,
        "active_references": [e.to_dict() for e in active],
        "excluded_references": [{"handle": e.handle, "role": e.role, "tier": e.tier, "reason": e.reason} for e in excluded],
        "annotations_summary": {k: v for k, v in summary.items() if k != "videos_used"},
        "notes": [
            "Retention, true 28-day analytics and per-video audience data are owner-only; they are not estimated.",
            "Editing traits (cut rate, captions, first visual) come only from human annotation files in references/annotations/.",
            "References are evidence, never templates; the writer is told to do better where the pattern would weaken the Short.",
        ],
    }
    return profile


def save_profile(cfg: Config, profile: dict[str, Any]) -> Path:
    path = profile_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def load_profile(cfg: Config) -> dict[str, Any] | None:
    path = profile_path(cfg)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("style profile %s unreadable; ignoring", path)
        return None
    return data if isinstance(data, dict) and "guidance" in data else None


def load_or_build_profile(cfg: Config) -> dict[str, Any] | None:
    """The profile the pipeline should use, or None when the style system is disabled."""
    if not bool(cfg.get("style.enabled", True)):
        return None
    profile = load_profile(cfg)
    if profile is None:
        try:
            profile = build_profile(cfg)
        except (FileNotFoundError, ValueError) as exc:
            log.warning("style profile unavailable (%s); editorial baseline only", exc)
            return None
        save_profile(cfg, profile)
    return profile


# --------------------------------------------------------------------------- #
# Pipeline-facing helpers
# --------------------------------------------------------------------------- #
def prompt_block(profile: dict[str, Any] | None) -> str:
    """Text handed to the script writer. Evidence is labelled; absence of evidence is stated."""
    if not profile:
        return ""
    lines = ["HOUSE STYLE (editorial baseline; references are evidence, not rules):"]
    lines += [f"- {p}" for p in profile.get("principles", [])]
    measured = [ev for ev in profile.get("active_references", []) if ev.get("signals")]
    if measured:
        lines.append("Evidence from high-performing technology Shorts (weighted by measured performance):")
        for ev in measured:
            lines.append(f"- {ev['handle']} (weight {ev['weight']:.1f}): {ev['reason']}")
        for note in profile.get("evidence_notes", [])[:8]:
            lines.append(f"- {note}")
    else:
        lines.append("No verified reference statistics are available; apply the editorial baseline only. Do not imitate any specific creator.")
    cadence = profile.get("guidance", {}).get("cadence", {})
    if cadence.get("source", "editorial_baseline") != "editorial_baseline":
        lines.append(f"Cadence evidence: aim for a visual change about every {cadence.get('target_segment_seconds')}s ({cadence.get('source')}).")
    hook_notes = profile.get("guidance", {}).get("hook", {}).get("notes", [])
    if hook_notes:
        lines.append("Hook evidence: " + " | ".join(hook_notes[:4]))
    lines.append(
        "Extract principles, never copy a video. If following a common reference pattern would make this Short weaker "
        "(retention, hook, clarity, originality, monetization safety), do the stronger thing instead."
    )
    return "\n".join(lines)


def apply_to_config(profile: dict[str, Any] | None, cfg: Config) -> list[str]:
    """Push concrete knobs (cadence, caption grouping) into the job config. Returns change notes."""
    if not profile:
        return []
    changes: list[str] = []
    cad = profile.get("guidance", {}).get("cadence", {})
    for key, cfg_key in (
        ("min_segment_seconds", "timeline.min_segment_seconds"),
        ("target_segment_seconds", "timeline.target_segment_seconds"),
        ("max_segment_seconds", "timeline.max_segment_seconds"),
    ):
        if key in cad and cad.get("source") != "editorial_baseline":
            old = cfg.get(cfg_key)
            if old != cad[key]:
                cfg.set(cfg_key, float(cad[key]))
                changes.append(f"{cfg_key}: {old} -> {cad[key]} ({cad.get('source')})")
    caps = profile.get("guidance", {}).get("captions", {})
    if caps.get("source", "editorial_baseline") != "editorial_baseline" and "max_words_per_caption" in caps:
        old = cfg.get("captions.max_words_per_caption")
        new = int(caps["max_words_per_caption"])
        if old != new:
            cfg.set("captions.max_words_per_caption", new)
            changes.append(f"captions.max_words_per_caption: {old} -> {new} ({caps.get('source')})")
    return changes


def influence_lines(profile: dict[str, Any] | None) -> list[str]:
    """Short human-readable log of what shaped this profile (for REVIEW.md and job logs)."""
    if not profile:
        return ["style system disabled; editorial baseline in prompts only"]
    out = [f"profile source: {profile.get('source')} (built {profile.get('built_at')})"]
    active = profile.get("active_references", [])
    if not any(ev.get("signals") for ev in active):
        out.append(
            "no measured references: statistics not fetched yet, so only the editorial baseline applied "
            "(provisional user-verified channels count only for annotation weighting)"
        )
    for ev in active:
        vids = ", ".join(f"{v['title'][:40]!r} ({v['views']:,} views)" for v in ev.get("top_videos", [])[:3])
        label = "measured" if ev.get("signals") else "provisional"
        out.append(f"{ev['handle']} weight {ev['weight']:.1f} ({ev['tier']}, {label}): {ev['reason']}" + (f"; top: {vids}" if vids else ""))
    for inf in profile.get("influences", []):
        if inf.get("video_id"):
            out.append(f"{inf['handle']} video {inf['video_id']} (w={inf['weight']}): {inf['why']}")
    for ex in profile.get("excluded_references", [])[:25]:
        out.append(f"excluded {ex['handle']} ({ex['role']}, {ex['tier']}): {ex['reason']}")
    return out


def channel_summary_rows(evaluations: list[ChannelEvaluation]) -> list[str]:
    rows = [f"{'channel':22s} {'role':9s} {'tier':11s} {'weight':6s} {'active':6s} reason"]
    for e in evaluations:
        rows.append(f"{e.handle:22s} {e.role:9s} {e.tier:11s} {e.weight:<6.2f} {'yes' if e.active else 'no':6s} {e.reason[:110]}")
    return rows


def stats_overview(stats: dict[str, ChannelStats]) -> list[str]:
    return [f"{h}: {len(s.shorts)} Shorts / {len(s.long_form)} long-form in sample, fetched {s.fetched_at} via {s.source}" for h, s in sorted(stats.items())]


def is_high(ev: ChannelEvaluation) -> bool:
    return ev.tier == TIER_HIGH
