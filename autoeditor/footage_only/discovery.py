"""Phase 1: job discovery.

Every sub-folder of the inbox is one job. Media files (video clips and
photographs) are collected, the optional ``topic.txt`` is read, and license
sidecars are noted. Original files are never modified.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from autoeditor.config import Config
from autoeditor.footage_only.photos import PHOTO_KIND, VIDEO_KIND, photo_extensions
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
    clips: list[Path]  # every media file (video and photo) in name order
    topic: str | None
    topic_file: Path | None
    ignored: list[str] = field(default_factory=list)
    credits: list[SourceCredit] = field(default_factory=list)
    kinds: dict[str, str] = field(default_factory=dict)  # file name -> "video" | "image"

    def kind_of(self, path: Path) -> str:
        return self.kinds.get(path.name, VIDEO_KIND)

    @property
    def videos(self) -> list[Path]:
        return [c for c in self.clips if self.kind_of(c) == VIDEO_KIND]

    @property
    def photos(self) -> list[Path]:
        return [c for c in self.clips if self.kind_of(c) == PHOTO_KIND]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "inbox_dir": str(self.inbox_dir),
            "clips": [str(c) for c in self.clips],
            "kinds": dict(self.kinds),
            "topic": self.topic,
            "topic_file": str(self.topic_file) if self.topic_file else None,
            "ignored": list(self.ignored),
            "credits": [asdict(c) for c in self.credits],
        }


def media_kind(path: Path, cfg: Config) -> str | None:
    """ "video", "image" or None for a file the editor does not accept."""
    ext = path.suffix.lower()
    if ext in {str(e).lower() for e in cfg.get("media.supported_extensions", [".mp4", ".mov", ".mkv", ".webm"])}:
        return VIDEO_KIND
    if ext in photo_extensions(cfg):
        return PHOTO_KIND
    return None


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
    clips: list[Path] = []
    kinds: dict[str, str] = {}
    ignored: list[str] = []
    for entry in sorted(job_dir.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(".") or entry.name.lower() in _IGNORED_NAMES:
            continue
        if entry.is_dir():
            ignored.append(f"{entry.name}/ (sub-folders are not scanned)")
            continue
        if entry.name == TOPIC_FILE:
            continue
        kind = media_kind(entry, cfg)
        if kind is not None:
            clips.append(entry)
            kinds[entry.name] = kind
        elif entry.suffix.lower() in {".json", ".txt"}:
            # license sidecars / manifests are consumed by credits, not media
            continue
        else:
            ignored.append(f"{entry.name} (unsupported extension)")
    topic, topic_file = read_topic(job_dir)
    credits = [find_license(clip, kind=kinds[clip.name]) for clip in clips]
    job = DiscoveredJob(
        name=sanitize_job_name(job_dir.name),
        inbox_dir=job_dir,
        clips=clips,
        topic=topic,
        topic_file=topic_file,
        ignored=ignored,
        credits=credits,
        kinds=kinds,
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
            log.warning("Job %s has no supported media files (video or photo); skipping", job.name)
            continue
        log.info(
            "Discovered job %s: %d video clip(s), %d photo(s), topic=%s",
            job.name,
            len(job.videos),
            len(job.photos),
            repr(job.topic) if job.topic else "<infer from footage>",
        )
        for note in job.ignored:
            log.info("  ignoring %s", note)
        jobs.append(job)
    if only and not jobs:
        raise FileNotFoundError(f"job '{only}' not found in {inbox} (or it has no media)")
    return jobs
