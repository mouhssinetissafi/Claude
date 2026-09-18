"""Video-level analysis from public metadata.

What the public API gives us per video: title, duration, publish time, views,
likes, comments. From that we can honestly compare a creator's outperforming
Shorts against their own baseline on: length, title/hook shape, engagement and
velocity. Editing traits that need the footage itself (cut rate, caption style,
first visual) come only from human annotation files, never from guesses.
"""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Any

from autoeditor.logging_utils import get_logger
from autoeditor.style.models import VideoStats

log = get_logger(__name__)

_DIGIT = re.compile(r"\d")
_VS = re.compile(r"\bvs\.?\b", re.IGNORECASE)


def title_features(title: str) -> dict[str, Any]:
    words = title.split()
    letters = [c for c in title if c.isalpha()]
    return {
        "question": title.strip().endswith("?"),
        "has_number": bool(_DIGIT.search(title)),
        "comparison": bool(_VS.search(title)),
        "caps_ratio": round(sum(1 for c in letters if c.isupper()) / len(letters), 2) if letters else 0.0,
        "word_count": len(words),
        "exclamation": "!" in title,
    }


def _share(videos: list[VideoStats], key: str) -> float:
    if not videos:
        return 0.0
    return sum(1 for v in videos if title_features(v.title)[key]) / len(videos)


def compare_top_vs_baseline(shorts: list[VideoStats], top_ratio: float = 2.0) -> list[str]:
    """Human-readable, number-backed findings; empty when the sample is too small to say anything."""
    scored = [v for v in shorts if v.outlier_score is not None]
    if len(scored) < 6:
        return []
    top = [v for v in scored if v.outlier_score is not None and v.outlier_score >= top_ratio]
    base = [v for v in scored if v not in top]
    if len(top) < 2 or len(base) < 3:
        return [f"no clear outperformers: {len(top)} Short(s) at >= {top_ratio}x median in a sample of {len(scored)}"]
    findings: list[str] = []
    top_len = statistics.median([v.duration_seconds for v in top])
    base_len = statistics.median([v.duration_seconds for v in base])
    findings.append(f"outperformers ({len(top)}) run a median {top_len:.0f}s vs {base_len:.0f}s for the baseline ({len(base)})")
    for key, label in (("has_number", "a number in the title"), ("question", "a question title"), ("comparison", "a 'vs' comparison title")):
        t, b = _share(top, key), _share(base, key)
        if abs(t - b) >= 0.25:
            findings.append(f"{label}: {t:.0%} of outperformers vs {b:.0%} of baseline")
    top_eng = [v.engagement_rate for v in top if v.engagement_rate is not None]
    base_eng = [v.engagement_rate for v in base if v.engagement_rate is not None]
    if top_eng and base_eng:
        te, be = statistics.median(top_eng), statistics.median(base_eng)
        if be > 0 and abs(te / be - 1) >= 0.2:
            findings.append(f"engagement rate {te:.2%} for outperformers vs {be:.2%} baseline")
    top_vph = [v.views_per_hour for v in top if v.views_per_hour is not None]
    base_vph = [v.views_per_hour for v in base if v.views_per_hour is not None]
    if top_vph and base_vph and statistics.median(base_vph) > 0:
        findings.append(f"views per hour {statistics.median(top_vph):,.0f} vs {statistics.median(base_vph):,.0f} baseline")
    return findings


# --------------------------------------------------------------------------- #
# Human annotations (editing traits that need the footage)
# --------------------------------------------------------------------------- #
ANNOTATION_FIELDS = {
    "hook_type": str,  # claim | question | contrast | demo | reveal | problem
    "first_visual_seconds": float,
    "avg_cut_seconds": float,
    "caption_style": str,  # word | phrase | none
    "emphasis": str,  # selective | heavy | none
    "visual_density": str,  # low | medium | high
    "structure": str,
    "payoff": str,
    "notes": str,
}


def load_annotations(directory: Path) -> dict[str, dict[str, Any]]:
    """Return {handle_lower: annotation document}. Malformed files are skipped with a warning."""
    out: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue  # templates
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("annotation %s skipped: %s", path.name, exc)
            continue
        if not isinstance(data, dict) or not data.get("handle") or not isinstance(data.get("videos"), list):
            log.warning("annotation %s skipped: needs 'handle' and a 'videos' list", path.name)
            continue
        out[str(data["handle"]).lstrip("@").lower()] = data
    return out


def summarize_annotations(
    annotations: dict[str, dict[str, Any]],
    channel_weights: dict[str, float],
    video_outliers: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """Weight each annotated video by channel weight x its outlier score (default 1.0)."""
    cut_samples: list[tuple[float, float]] = []
    first_visual: list[tuple[float, float]] = []
    hooks: dict[str, float] = {}
    captions: dict[str, float] = {}
    emphasis: dict[str, float] = {}
    density: dict[str, float] = {}
    used: list[dict[str, Any]] = []
    for handle, doc in annotations.items():
        cw = channel_weights.get(handle, 0.0)
        if cw <= 0:
            continue
        for video in doc.get("videos", []):
            vid = str(video.get("video_id", ""))
            outlier = float(video_outliers.get(handle, {}).get(vid, 1.0))
            w = cw * max(0.5, min(outlier, 5.0))
            if isinstance(video.get("avg_cut_seconds"), (int, float)):
                cut_samples.append((float(video["avg_cut_seconds"]), w))
            if isinstance(video.get("first_visual_seconds"), (int, float)):
                first_visual.append((float(video["first_visual_seconds"]), w))
            for key, bucket in (("hook_type", hooks), ("caption_style", captions), ("emphasis", emphasis), ("visual_density", density)):
                val = video.get(key)
                if isinstance(val, str) and val:
                    bucket[val] = bucket.get(val, 0.0) + w
            used.append({"handle": handle, "video_id": vid, "weight": round(w, 2), "notes": str(video.get("notes", ""))[:200]})

    def wmean(samples: list[tuple[float, float]]) -> float | None:
        total = sum(w for _, w in samples)
        return round(sum(v * w for v, w in samples) / total, 2) if total > 0 else None

    def top(bucket: dict[str, float]) -> str | None:
        return max(bucket.items(), key=lambda kv: kv[1])[0] if bucket else None

    return {
        "annotated_videos": len(used),
        "avg_cut_seconds": wmean(cut_samples),
        "first_visual_seconds": wmean(first_visual),
        "dominant_hook_type": top(hooks),
        "dominant_caption_style": top(captions),
        "dominant_emphasis": top(emphasis),
        "dominant_visual_density": top(density),
        "videos_used": used,
    }
