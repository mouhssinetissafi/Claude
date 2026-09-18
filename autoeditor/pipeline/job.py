"""Per-job directory layout.

All intermediate files live under ``work/<job>/``; deliverables under
``output/<job>/``; cache under ``cache/<job>/``; failed QC copies under
``review/<job>/``. Paths inside timeline.json are *relative to the work dir*
so the Remotion staging step can copy them verbatim.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoeditor.config import Config

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_job_name(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", name.strip()).strip("._-")
    return cleaned or "job"


@dataclass(frozen=True)
class JobPaths:
    name: str
    work: Path
    output: Path
    cache: Path
    review: Path
    inbox: Path | None = None

    @classmethod
    def for_job(cls, cfg: Config, name: str, inbox: Path | None = None) -> JobPaths:
        safe = sanitize_job_name(name)
        return cls(
            name=safe,
            work=cfg.work_dir / safe,
            output=cfg.output_dir / safe,
            cache=cfg.cache_dir / safe,
            review=cfg.review_dir / safe,
            inbox=inbox,
        )

    def ensure(self) -> None:
        for d in (self.work, self.output, self.cache, self.normalized_dir, self.frames_dir, self.audio_dir, self.media_dir):
            d.mkdir(parents=True, exist_ok=True)

    # work/ ------------------------------------------------------------------
    @property
    def normalized_dir(self) -> Path:
        return self.work / "normalized"

    @property
    def frames_dir(self) -> Path:
        return self.work / "frames"

    @property
    def audio_dir(self) -> Path:
        return self.work / "audio"

    @property
    def media_dir(self) -> Path:
        return self.work / "media"

    @property
    def media_manifest_json(self) -> Path:
        return self.work / "media_manifest.json"

    @property
    def scenes_json(self) -> Path:
        return self.work / "scenes.json"

    @property
    def inventory_json(self) -> Path:
        return self.work / "inventory.json"

    @property
    def script_json(self) -> Path:
        return self.work / "script.json"

    @property
    def voice_timing_json(self) -> Path:
        return self.work / "voice_timing.json"

    @property
    def voice_audio(self) -> Path:
        return self.audio_dir / "voice.mp3"

    @property
    def captions_json(self) -> Path:
        return self.work / "captions.json"

    @property
    def timeline_json(self) -> Path:
        return self.work / "timeline.json"

    @property
    def render_props_json(self) -> Path:
        return self.work / "render_props.json"

    @property
    def state_json(self) -> Path:
        return self.work / "job_state.json"

    @property
    def log_file(self) -> Path:
        return self.work / "job.log"

    @property
    def render_raw_mp4(self) -> Path:
        return self.work / "render_raw.mp4"

    # output/ ----------------------------------------------------------------
    @property
    def final_mp4(self) -> Path:
        return self.output / "final.mp4"

    @property
    def qc_json(self) -> Path:
        return self.output / "qc.json"

    @property
    def metadata_json(self) -> Path:
        return self.output / "metadata.json"

    @property
    def credits_txt(self) -> Path:
        return self.output / "credits.txt"

    @property
    def thumbnail_jpg(self) -> Path:
        return self.output / "thumbnail_base.jpg"

    def rel(self, path: Path) -> str:
        """Path relative to the work dir, POSIX style, for timeline.json."""
        return path.resolve().relative_to(self.work.resolve()).as_posix()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)
