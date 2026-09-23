"""ffprobe wrapper and parser.

``parse_ffprobe_json`` is pure (no subprocess) so it can be unit-tested with
fixture output; ``probe`` runs ffprobe and feeds it through the parser.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any


class ProbeError(RuntimeError):
    """ffprobe failed or produced unusable output (corrupt/unsupported media)."""


@dataclass
class MediaInfo:
    filename: str
    path: str
    duration: float
    width: int
    height: int
    fps: float
    codec: str
    aspect_ratio: float
    has_audio: bool
    has_video: bool
    size_bytes: int = 0
    format_name: str = ""
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    rotation: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ffprobe_binary() -> str | None:
    explicit = os.environ.get("AUTOEDITOR_FFPROBE", "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    return shutil.which("ffprobe")


def ffprobe_available() -> bool:
    return _ffprobe_binary() is not None


def _parse_rate(raw: str | None) -> float:
    if not raw or raw in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(raw))
    except (ValueError, ZeroDivisionError):
        try:
            return float(raw)
        except ValueError:
            return 0.0


def _rotation(stream: dict[str, Any]) -> int:
    tags = stream.get("tags") or {}
    rot = tags.get("rotate")
    if rot is None:
        for sd in stream.get("side_data_list") or []:
            if "rotation" in sd:
                rot = sd["rotation"]
                break
    try:
        return int(round(float(rot))) % 360 if rot is not None else 0
    except (TypeError, ValueError):
        return 0


def parse_ffprobe_json(payload: dict[str, Any], path: Path | str) -> MediaInfo:
    """Convert ffprobe ``-show_format -show_streams`` JSON into MediaInfo."""
    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    warnings: list[str] = []
    if video is None:
        raise ProbeError(f"no video stream in {path}")

    duration = 0.0
    for src in (video.get("duration"), fmt.get("duration")):
        if src is None or src == "N/A":
            continue
        try:
            duration = float(src)
        except (TypeError, ValueError):
            duration = 0.0
        if duration > 0:
            break
    if duration <= 0:
        warnings.append("duration missing or zero")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    rotation = _rotation(video)
    if rotation in (90, 270):
        width, height = height, width
        warnings.append(f"rotation metadata {rotation} applied")
    if width <= 0 or height <= 0:
        raise ProbeError(f"invalid dimensions {width}x{height} in {path}")

    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    if fps <= 0:
        warnings.append("frame rate missing")

    path_obj = Path(path)
    size = 0
    try:
        size = int(fmt.get("size") or 0)
    except (TypeError, ValueError):
        size = 0

    return MediaInfo(
        filename=path_obj.name,
        path=str(path_obj),
        duration=round(duration, 3),
        width=width,
        height=height,
        fps=round(fps, 3),
        codec=str(video.get("codec_name") or "unknown"),
        aspect_ratio=round(width / height, 4) if height else 0.0,
        has_audio=audio is not None,
        has_video=True,
        size_bytes=size,
        format_name=str(fmt.get("format_name") or ""),
        audio_codec=str(audio.get("codec_name")) if audio else None,
        sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        channels=int(audio["channels"]) if audio and audio.get("channels") else None,
        rotation=rotation,
        warnings=warnings,
    )


def probe(path: Path, timeout: int = 60) -> MediaInfo:
    """Run ffprobe on ``path`` and parse the result."""
    if not ffprobe_available():
        raise ProbeError("ffprobe is not installed or not on PATH")
    if not path.exists():
        raise ProbeError(f"file does not exist: {path}")
    binary = _ffprobe_binary()
    if not binary:
        raise ProbeError("ffprobe is not installed or not on PATH")
    cmd = [
        binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out on {path}") from exc
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed on {path.name}: {proc.stderr.strip()[:400]}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {path.name}") from exc
    return parse_ffprobe_json(payload, path)


def audio_duration(path: Path, timeout: int = 60) -> float:
    """Duration of an audio (or video) file in seconds."""
    if not ffprobe_available():
        raise ProbeError("ffprobe is not installed or not on PATH")
    binary = _ffprobe_binary()
    if not binary:
        raise ProbeError("ffprobe is not installed or not on PATH")
    cmd = [
        binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed on {path.name}: {proc.stderr.strip()[:400]}")
    try:
        return round(float(proc.stdout.strip()), 3)
    except ValueError as exc:
        raise ProbeError(f"could not read duration of {path.name}") from exc
