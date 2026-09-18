"""Phase 1: job discovery.

Every sub-folder of the inbox is one job. Media files are collected, the
optional ``topic.txt`` is read, and license sidecars are noted. Original files
are never modified.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from autoeditor.config import Config
from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.credits import SourceCredit, find_license
from autoeditor.pipeline.job import sanitize_job_name

log = get_logger(__name__)

TOPIC_FILE = "topic.txt"
_IGNORED_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}


@dataclass
class DiscoveredJob:
    name: str
    inbox_dir: Path
    clips: list[Path]
    topic: str | None
    topic_file: Path | None
    ignored: list[str] = field(default_factory=list)
    credits: list[SourceCredit] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "inbox_dir": str(self.inbox_dir),
            "clips": [str(c) for c in self.clips],
            "topic": self.topic,
            "topic_file": str(self.topic_file) if self.topic_file else None,
            "ignored": list(self.ignored),
            "credits": [asdict(c) for c in self.credits],
        }


def read_topic(job_dir: Path) -> tuple[str | None, Path | None]:
    """Return (topic, path). Missing or blank topic.txt -> (None, None)."""
    path = job_dir / TOPIC_FILE
    if not path.exists():
        return None, None
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        log.info("topic.txt in %s is empty; story will be inferred from footage", job_dir.name)
        return None, path
    return " ".join(text.split()), path


def discover_job(job_dir: Path, cfg: Config) -> DiscoveredJob:
    exts = {e.lower() for e in cfg.get("media.supported_extensions", [".mp4", ".mov", ".mkv", ".webm"])}
    clips: list[Path] = []
    ignored: list[str] = []
    for entry in sorted(job_dir.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(".") or entry.name.lower() in _IGNORED_NAMES:
            continue
        if entry.is_dir():
            ignored.append(f"{entry.name}/ (sub-folders are not scanned)")
            continue
        if entry.name == TOPIC_FILE:
            continue
        if entry.suffix.lower() in exts:
            clips.append(entry)
        elif entry.suffix.lower() in {".json", ".txt"}:
            # license sidecars / manifests are consumed by credits, not media
            continue
        else:
            ignored.append(f"{entry.name} (unsupported extension)")
    topic, topic_file = read_topic(job_dir)
    credits = [find_license(clip, kind="video") for clip in clips]
    job = DiscoveredJob(
        name=sanitize_job_name(job_dir.name),
        inbox_dir=job_dir,
        clips=clips,
        topic=topic,
        topic_file=topic_file,
        ignored=ignored,
        credits=credits,
    )
    return job


def discover_jobs(inbox: Path, cfg: Config, *, only: str | None = None) -> list[DiscoveredJob]:
    if not inbox.exists() or not inbox.is_dir():
        raise FileNotFoundError(f"inbox folder not found: {inbox}")
    jobs: list[DiscoveredJob] = []
    for job_dir in sorted(p for p in inbox.iterdir() if p.is_dir() and not p.name.startswith(".")):
        if only and job_dir.name != only and sanitize_job_name(job_dir.name) != sanitize_job_name(only):
            continue
        job = discover_job(job_dir, cfg)
        if not job.clips:
            log.warning("Job %s has no supported media files; skipping", job.name)
            continue
        log.info("Discovered job %s: %d clip(s), topic=%s", job.name, len(job.clips), repr(job.topic) if job.topic else "<infer from footage>")
        for note in job.ignored:
            log.info("  ignoring %s", note)
        jobs.append(job)
    if only and not jobs:
        raise FileNotFoundError(f"job '{only}' not found in {inbox} (or it has no media)")
    return jobs
