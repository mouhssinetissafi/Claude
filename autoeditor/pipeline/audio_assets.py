"""Music and SFX selection from the local assets library.

Tracks are chosen deterministically per job (hash of the job name) so re-runs
are stable. Selected files are copied into ``work/<job>/audio/`` so timeline
paths stay relative to the work dir. License sidecars are carried into credits.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from autoeditor.config import Config
from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.credits import SourceCredit, find_license
from autoeditor.pipeline.job import JobPaths

log = get_logger(__name__)

_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def _audio_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _AUDIO_EXT)


def _pick(files: list[Path], seed: str) -> Path:
    idx = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % len(files)
    return files[idx]


def select_music(paths: JobPaths, cfg: Config) -> tuple[dict[str, Any] | None, SourceCredit | None]:
    if not bool(cfg.get("audio.music_enabled", True)):
        return None, None
    folder = Path(cfg.get("audio.music_dir", "assets/music"))
    folder = folder if folder.is_absolute() else cfg.root / folder
    files = _audio_files(folder)
    if not files:
        log.info("No music tracks in %s; rendering without music", folder)
        return None, None
    track = _pick(files, paths.name)
    dst = paths.audio_dir / f"music{track.suffix.lower()}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(track, dst)
    music = {
        "src": paths.rel(dst),
        "volume": float(cfg.get("audio.music_volume", 0.12)),
        "ducking": bool(cfg.get("audio.ducking", True)),
        "ducking_volume": float(cfg.get("audio.ducking_volume", 0.05)),
    }
    credit = find_license(track, kind="music")
    return music, credit


def select_sfx(paths: JobPaths, cfg: Config, *, line_starts: list[float]) -> tuple[list[dict[str, Any]], list[SourceCredit]]:
    """Place one transition SFX at every line start after the first, if any SFX exist."""
    if not bool(cfg.get("audio.sfx_enabled", True)):
        return [], []
    folder = Path(cfg.get("audio.sfx_dir", "assets/sfx"))
    folder = folder if folder.is_absolute() else cfg.root / folder
    files = _audio_files(folder)
    if not files or len(line_starts) < 2:
        return [], []
    sfx_dir = paths.audio_dir / "sfx"
    sfx_dir.mkdir(parents=True, exist_ok=True)
    whoosh = next((f for f in files if "whoosh" in f.name.lower() or "swipe" in f.name.lower()), files[0])
    dst = sfx_dir / whoosh.name
    shutil.copy2(whoosh, dst)
    placements = [{"src": paths.rel(dst), "at": round(t, 3), "volume": float(cfg.get("audio.sfx_volume", 0.35))} for t in line_starts[1:]]
    return placements, [find_license(whoosh, kind="sfx")]
