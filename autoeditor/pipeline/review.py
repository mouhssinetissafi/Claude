"""Human review gate: REVIEW.md checklist, AI-disclosure flag and approval marker.

Nothing is published automatically. After QC the job waits for a human who
reads ``output/<job>/REVIEW.md`` and creates ``output/<job>/APPROVED`` to
allow an upload with ``--upload``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.credits import SourceCredit
from autoeditor.pipeline.job import JobPaths

log = get_logger(__name__)

APPROVAL_FILE = "APPROVED"
REVIEW_FILE = "REVIEW.md"


class NeedsReviewError(RuntimeError):
    """Raised when a job must stop and wait for a human instead of producing a weak video."""


@dataclass
class ReviewInfo:
    job: str
    title: str
    reason: str | None = None  # why the job stopped early, if it did
    duration_seconds: float | None = None
    min_duration_seconds: float | None = None
    facts_to_verify: list[str] = field(default_factory=list)
    license_warnings: list[str] = field(default_factory=list)
    originality: dict[str, Any] | None = None
    ai_disclosure: dict[str, Any] | None = None
    qc: dict[str, Any] | None = None
    variation: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)


def ai_disclosure(credits: list[SourceCredit]) -> dict[str, Any]:
    """Flag footage that its metadata marks as AI-generated or meaningfully altered.

    Scripting, captions and editing assistance by AI are production help and do
    not by themselves trigger the flag; only the footage shown does.
    """
    flagged = [c.file for c in credits if c.kind in {"video", "image"} and (c.ai_generated or c.altered)]
    if flagged:
        return {
            "review_required": True,
            "flagged_files": flagged,
            "note": "Footage metadata marks these files as AI-generated or meaningfully altered. YouTube may require an "
            "'altered or synthetic content' disclosure when realistic AI or altered footage is used. Decide before publishing.",
        }
    return {
        "review_required": False,
        "flagged_files": [],
        "note": "No footage is marked as AI-generated or altered. AI was used for scripting, captions and editing assistance only, "
        "which YouTube does not treat as requiring a synthetic-content disclosure by itself.",
    }


def approval_status(paths: JobPaths) -> tuple[bool, str]:
    marker = paths.output / APPROVAL_FILE
    if marker.exists():
        note = marker.read_text(encoding="utf-8", errors="replace").strip()
        return True, note or "approved"
    return False, f"not approved: create {marker} after reviewing {paths.output / REVIEW_FILE}"


def render_review(info: ReviewInfo, paths: JobPaths) -> str:
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    lines = [f"# Review checklist: {info.title}", "", f"Job: `{info.job}`  ", f"Generated: {stamp}", ""]
    if info.reason:
        lines += ["## STOPPED - needs a human decision", "", f"**{info.reason}**", "", "Fix the input (more footage, different topic) and re-run the job.", ""]
    lines += ["## Status", ""]
    if info.duration_seconds is not None:
        ok = info.min_duration_seconds is None or info.duration_seconds >= info.min_duration_seconds
        lines.append(f"- Duration: {info.duration_seconds:.1f}s (minimum {info.min_duration_seconds or 0:.0f}s) {'OK' if ok else 'TOO SHORT'}")
    if info.qc is not None:
        failed = [k for k, v in info.qc.get("checks", {}).items() if not v.get("passed")]
        lines.append(f"- QC: {'passed' if info.qc.get('passed') else 'FAILED (' + ', '.join(failed) + ')'}")
    if info.variation:
        lines.append(
            f"- Variation profile: hook={info.variation.get('hook_style', '').split(':')[0]}, structure={info.variation.get('structure', '').split(':')[0]}, ending={info.variation.get('ending', '').split(':')[0]}"
        )
    lines.append("")
    lines += ["## 1. Facts to verify before publishing", ""]
    lines += [f"- [ ] {f}" for f in info.facts_to_verify] or ["- none flagged"]
    lines += ["", "## 2. Licenses", ""]
    lines += [f"- [ ] {w}" for w in info.license_warnings] or ["- all used footage has license information (see credits.txt)"]
    lines += ["", "## 3. Originality", ""]
    if info.originality:
        lines.append(f"- verdict: **{info.originality.get('verdict', 'ok')}** (max similarity to another job: {info.originality.get('max_similarity', 0):.0%})")
        lines += [f"- {n}" for n in info.originality.get("notes", [])]
    else:
        lines.append("- not checked")
    lines += ["", "## 4. AI disclosure", ""]
    if info.ai_disclosure:
        flag = "**REVIEW REQUIRED**" if info.ai_disclosure.get("review_required") else "not required by footage"
        lines.append(f"- {flag}: {info.ai_disclosure.get('note', '')}")
        for f in info.ai_disclosure.get("flagged_files", []):
            lines.append(f"  - [ ] {f}")
    if info.warnings:
        lines += ["", "## 5. Warnings", ""] + [f"- {w}" for w in info.warnings]
    lines += [
        "",
        "## Approve",
        "",
        "Watch `final.mp4`, tick the boxes above, then create the approval marker to allow uploading:",
        "",
        "```",
        f'echo "approved by <your name>" > {paths.output / APPROVAL_FILE}',
        f"python run.py --footage-only --job {info.job} --upload",
        "```",
        "",
        "Uploads never happen without this file, a passed QC, `upload.enabled: true` and `--upload`.",
        "",
    ]
    return "\n".join(lines)


def write_review(info: ReviewInfo, paths: JobPaths) -> Path:
    paths.output.mkdir(parents=True, exist_ok=True)
    target = paths.output / REVIEW_FILE
    target.write_text(render_review(info, paths), encoding="utf-8")
    return target
