"""Originality safeguards: per-job variation profiles and a duplicate registry.

These are *production-quality* measures, not detection evasion: they make each
Short an original piece of work (varied hook, structure, ending and scene
choice) and refuse to mass-produce near-identical videos.

* ``variation_profile`` derives a deterministic style brief per job so two jobs
  never get the same hook/intro/outro pattern by default.
* ``OriginalityRegistry`` remembers a text fingerprint (word 3-shingles) and the
  footage signature of every produced script; a new script that is too similar
  to an earlier job is flagged (warn) or rejected for human review.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autoeditor.logging_utils import get_logger

log = get_logger(__name__)

_WORD = re.compile(r"[a-z0-9']+")

HOOK_STYLES = [
    "contrast: open on what people expect, then show what the footage actually reveals",
    "detail-first: open on one specific visual detail and let it carry the first line",
    "consequence-first: open on the result, then work back to the cause",
    "quiet-observation: open calmly on a single plain statement, no drama",
    "problem-first: open on the tension or trade-off, then show how it plays out",
    "reveal: open on a surface description, hold the key point for line two",
]
STRUCTURES = [
    "escalation: each line raises the stakes or adds a sharper detail",
    "three-beat: setup, complication, resolution",
    "before-after: what it was, what changed, what it means now",
    "cause-effect: one decision and the chain of consequences it started",
    "tour: move through the footage spatially, one area or angle per beat",
]
ENDINGS = [
    "loop-to-opener: the last line echoes the first so the Short loops cleanly",
    "one-line-takeaway: end on a single plain sentence that states the point",
    "open-question: end on a real question the viewer can answer in comments",
    "quiet-button: end on a small concrete detail, no summary",
    "callback-detail: end by returning to a detail shown mid-video",
]
PACINGS = ["brisk", "measured"]


def _digest(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], 16)


def variation_profile(seed_material: str, *, attempt: int = 0) -> dict[str, Any]:
    """Deterministic style brief for a job. ``attempt`` shifts every choice for a retry."""
    seed = _digest(f"{seed_material}|attempt={attempt}")
    return {
        "hook_style": HOOK_STYLES[seed % len(HOOK_STYLES)],
        "structure": STRUCTURES[(seed // 7) % len(STRUCTURES)],
        "ending": ENDINGS[(seed // 53) % len(ENDINGS)],
        "pacing": PACINGS[(seed // 311) % len(PACINGS)],
        "opener_rotation": (seed // 1009) % 5,
        "attempt": attempt,
    }


def variation_instruction(profile: dict[str, Any]) -> str:
    return (
        "VARIATION PROFILE for this video (follow it so this Short does not resemble other videos):\n"
        f"- hook style: {profile['hook_style']}\n"
        f"- structure: {profile['structure']}\n"
        f"- ending: {profile['ending']}\n"
        f"- pacing: {profile['pacing']}\n"
        "Write an original narrative in your own framing. Never reproduce, closely paraphrase or lightly reword an "
        "article, press release, product page or another creator's script. Do not reuse a stock hook, intro or outro."
    )


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #
def normalize_words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def shingles(words: list[str], n: int = 3) -> set[str]:
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def script_fingerprint(script: dict[str, Any]) -> dict[str, Any]:
    lines = [str(line.get("narration", "")) for line in script.get("lines", [])]
    words = normalize_words(" ".join(lines))
    hook = " ".join(normalize_words(lines[0])) if lines else ""
    return {
        "hook": hook,
        "shingles": sorted(hashlib.sha1(s.encode("utf-8")).hexdigest()[:12] for s in shingles(words)),
        "word_count": len(words),
        "title": str(script.get("title", "")),
    }


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
@dataclass
class OriginalityReport:
    verdict: str  # ok | warn | reject
    max_similarity: float = 0.0
    most_similar_job: str | None = None
    same_hook_jobs: list[str] = field(default_factory=list)
    same_footage_jobs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OriginalityRegistry:
    """JSON file remembering every produced script, keyed by job name."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("jobs"), dict):
                    self.entries = loaded["jobs"]
            except (OSError, json.JSONDecodeError):
                log.warning("originality registry %s is unreadable; starting empty", path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "jobs": self.entries}, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def check(self, job: str, fingerprint: dict[str, Any], footage: list[str], *, warn_at: float = 0.5, reject_at: float = 0.8) -> OriginalityReport:
        report = OriginalityReport(verdict="ok")
        my_shingles = set(fingerprint.get("shingles", []))
        my_footage = set(footage)
        for other_job, entry in self.entries.items():
            if other_job == job:
                continue  # re-running the same job is not a duplicate of itself
            sim = jaccard(my_shingles, entry.get("shingles", []))
            if sim > report.max_similarity:
                report.max_similarity = round(sim, 3)
                report.most_similar_job = other_job
            if fingerprint.get("hook") and fingerprint.get("hook") == entry.get("hook"):
                report.same_hook_jobs.append(other_job)
            other_footage = set(entry.get("footage", []))
            if my_footage and other_footage and my_footage == other_footage:
                report.same_footage_jobs.append(other_job)
        if report.max_similarity >= reject_at:
            report.verdict = "reject"
            report.notes.append(f"script is {report.max_similarity:.0%} similar to job '{report.most_similar_job}' (limit {reject_at:.0%})")
        elif report.max_similarity >= warn_at:
            report.verdict = "warn"
            report.notes.append(f"script is {report.max_similarity:.0%} similar to job '{report.most_similar_job}'")
        if report.same_hook_jobs:
            report.notes.append("opening line already used by: " + ", ".join(report.same_hook_jobs))
            if report.verdict == "ok":
                report.verdict = "warn"
        if report.same_footage_jobs and report.max_similarity >= warn_at:
            report.notes.append("same footage AND similar script as: " + ", ".join(report.same_footage_jobs) + " - near-duplicate video")
            report.verdict = "reject"
        elif report.same_footage_jobs:
            report.notes.append("same footage already used by: " + ", ".join(report.same_footage_jobs) + " (script differs)")
        return report

    def record(self, job: str, fingerprint: dict[str, Any], footage: list[str], *, title: str, variation: dict[str, Any] | None) -> None:
        self.entries[job] = {
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "title": title,
            "hook": fingerprint.get("hook", ""),
            "shingles": list(fingerprint.get("shingles", [])),
            "word_count": int(fingerprint.get("word_count", 0)),
            "footage": sorted(set(footage)),
            "variation": {k: v for k, v in (variation or {}).items() if k != "attempt"},
        }
        self.save()
