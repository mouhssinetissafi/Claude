"""FFmpeg operations used by both pipeline modes.

Every function shells out to ``ffmpeg`` and raises FFmpegError on failure; the
parsers (``parse_blackdetect``, ``parse_silencedetect``, ``parse_volumedetect``,
``parse_loudnorm``) are pure so they can be tested without ffmpeg.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from autoeditor.logging_utils import get_logger

log = get_logger(__name__)


class FFmpegError(RuntimeError):
    """ffmpeg exited with an error."""


def _ffmpeg_binary() -> str | None:
    explicit = os.environ.get("AUTOEDITOR_FFMPEG", "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    return shutil.which("ffmpeg")


def ffmpeg_available() -> bool:
    return _ffmpeg_binary() is not None


def run_ffmpeg(args: Sequence[str], *, timeout: int = 1800, description: str = "ffmpeg") -> str:
    """Run ffmpeg with ``args`` (without the leading binary). Returns stderr text."""
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg is not installed or not on PATH")
    binary = _ffmpeg_binary()
    if not binary:
        raise FFmpegError("ffmpeg is not installed or not on PATH")
    cmd = [binary, "-hide_banner", "-nostdin", "-y", *args]
    log.debug("%s: %s", description, " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"{description} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-8:]
        raise FFmpegError(f"{description} failed (exit {proc.returncode}): " + " | ".join(tail))
    return proc.stderr


# --------------------------------------------------------------------------- #
# Video normalization (Phase 2)
# --------------------------------------------------------------------------- #
def normalize_video(
    src: Path,
    dst: Path,
    *,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    crf: int = 18,
    preset: str = "medium",
    pix_fmt: str = "yuv420p",
    codec: str = "libx264",
    strip_audio: bool = True,
) -> Path:
    """Scale+crop to the target frame without stretching, constant fps, no audio."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps}"
    args = [
        "-i",
        str(src),
        "-vf",
        vf,
        "-c:v",
        codec,
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        pix_fmt,
        "-movflags",
        "+faststart",
    ]
    if strip_audio:
        args.append("-an")
    args.append(str(dst))
    run_ffmpeg(args, description=f"normalize {src.name}")
    return dst


# --------------------------------------------------------------------------- #
# Frames (Phase 3 / Phase 12)
# --------------------------------------------------------------------------- #
def extract_frame(src: Path, at_seconds: float, dst: Path, *, width: int | None = None, quality: int = 3) -> Path:
    """Grab one frame at ``at_seconds`` as JPEG."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = ["-ss", f"{max(0.0, at_seconds):.3f}", "-i", str(src), "-frames:v", "1", "-q:v", str(quality)]
    if width:
        args += ["-vf", f"scale={width}:-2"]
    args.append(str(dst))
    run_ffmpeg(args, timeout=120, description=f"frame {src.name}@{at_seconds:.2f}")
    return dst


def detect_scene_changes_ffmpeg(src: Path, threshold: float = 0.4, timeout: int = 900) -> list[float]:
    """Fallback scene detection using ffmpeg's ``scene`` score. Returns cut timestamps."""
    args = [
        "-i",
        str(src),
        "-filter:v",
        f"select='gt(scene,{threshold})',showinfo",
        "-an",
        "-f",
        "null",
        "-",
    ]
    stderr = run_ffmpeg(args, timeout=timeout, description=f"scene-detect {src.name}")
    cuts: list[float] = []
    for m in re.finditer(r"pts_time:([0-9.]+)", stderr):
        cuts.append(round(float(m.group(1)), 3))
    return cuts


# --------------------------------------------------------------------------- #
# Audio (Phase 7)
# --------------------------------------------------------------------------- #
def trim_silence(src: Path, dst: Path, *, threshold_db: float = -45, keep_ms: int = 40) -> Path:
    """Trim leading/trailing silence so concatenated lines do not have dead air."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    thr = f"{threshold_db}dB"
    af = (
        f"silenceremove=start_periods=1:start_threshold={thr}:start_silence={keep_ms / 1000:.3f},"
        f"areverse,silenceremove=start_periods=1:start_threshold={thr}:start_silence={keep_ms / 1000:.3f},areverse"
    )
    run_ffmpeg(["-i", str(src), "-af", af, str(dst)], timeout=300, description=f"trim {src.name}")
    return dst


def concat_audio(parts: Sequence[Path], dst: Path, *, gap_seconds: float = 0.0, sample_rate: int = 44100) -> Path:
    """Concatenate audio files, inserting a short silence between them.

    Uses the concat filter (re-encodes) so mixed input formats are fine.
    """
    if not parts:
        raise FFmpegError("concat_audio called with no inputs")
    dst.parent.mkdir(parents=True, exist_ok=True)
    args: list[str] = []
    for p in parts:
        args += ["-i", str(p)]
    n = len(parts)
    filters: list[str] = []
    labels: list[str] = []
    for i in range(n):
        lbl = f"a{i}"
        chain = f"[{i}:a]aformat=sample_rates={sample_rate}:channel_layouts=mono"
        if gap_seconds > 0 and i < n - 1:
            chain += f",apad=pad_dur={gap_seconds:.3f}"
        filters.append(f"{chain}[{lbl}]")
        labels.append(f"[{lbl}]")
    filters.append("".join(labels) + f"concat=n={n}:v=0:a=1[out]")
    args += ["-filter_complex", ";".join(filters), "-map", "[out]"]
    if dst.suffix.lower() == ".mp3":
        args += ["-c:a", "libmp3lame", "-b:a", "192k"]
    elif dst.suffix.lower() in {".m4a", ".aac"}:
        args += ["-c:a", "aac", "-b:a", "192k"]
    args.append(str(dst))
    run_ffmpeg(args, timeout=600, description="concat narration")
    return dst


def convert_audio(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = ["-i", str(src)]
    if dst.suffix.lower() == ".mp3":
        args += ["-c:a", "libmp3lame", "-b:a", "192k"]
    args.append(str(dst))
    run_ffmpeg(args, timeout=300, description=f"convert {src.name}")
    return dst


# --------------------------------------------------------------------------- #
# QC analysis (Phase 11)
# --------------------------------------------------------------------------- #
@dataclass
class Interval:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


_BLACK_RE = re.compile(r"black_start:([0-9.]+)\s+black_end:([0-9.]+)")
_SIL_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SIL_END_RE = re.compile(r"silence_end:\s*(-?[0-9.]+)")
_VOL_RE = re.compile(r"(max_volume|mean_volume):\s*(-?[0-9.]+)\s*dB")


def parse_blackdetect(stderr: str) -> list[Interval]:
    return [Interval(float(a), float(b)) for a, b in _BLACK_RE.findall(stderr)]


def parse_silencedetect(stderr: str, total_duration: float | None = None) -> list[Interval]:
    starts = [float(x) for x in _SIL_START_RE.findall(stderr)]
    ends = [float(x) for x in _SIL_END_RE.findall(stderr)]
    intervals: list[Interval] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else (total_duration if total_duration is not None else s)
        intervals.append(Interval(max(0.0, s), max(0.0, e)))
    return intervals


def parse_volumedetect(stderr: str) -> dict[str, float]:
    return {k: float(v) for k, v in _VOL_RE.findall(stderr)}


def parse_loudnorm(stderr: str) -> dict[str, str]:
    """Extract the JSON block printed by ``loudnorm=print_format=json``."""
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise FFmpegError("loudnorm did not print a JSON block")
    try:
        parsed = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError as exc:
        raise FFmpegError("loudnorm JSON was malformed") from exc
    return {str(k): str(v) for k, v in parsed.items()}


def detect_black(src: Path, *, min_duration: float = 1.0, pixel_threshold: float = 0.10) -> list[Interval]:
    stderr = run_ffmpeg(
        ["-i", str(src), "-vf", f"blackdetect=d={min_duration}:pix_th={pixel_threshold}", "-an", "-f", "null", "-"],
        timeout=900,
        description="blackdetect",
    )
    return parse_blackdetect(stderr)


def detect_silence(src: Path, *, noise_db: float = -45, min_duration: float = 2.0) -> list[Interval]:
    stderr = run_ffmpeg(
        ["-i", str(src), "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}", "-vn", "-f", "null", "-"],
        timeout=900,
        description="silencedetect",
    )
    return parse_silencedetect(stderr)


def measure_volume(src: Path) -> dict[str, float]:
    stderr = run_ffmpeg(["-i", str(src), "-af", "volumedetect", "-vn", "-f", "null", "-"], timeout=900, description="volumedetect")
    return parse_volumedetect(stderr)


def measure_loudness(src: Path, *, target_lufs: float = -14, true_peak: float = -1.0, lra: float = 11) -> dict[str, str]:
    stderr = run_ffmpeg(
        [
            "-i",
            str(src),
            "-af",
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}:print_format=json",
            "-vn",
            "-f",
            "null",
            "-",
        ],
        timeout=900,
        description="loudnorm-measure",
    )
    return parse_loudnorm(stderr)


def normalize_loudness(
    src: Path,
    dst: Path,
    *,
    target_lufs: float = -14,
    true_peak: float = -1.0,
    lra: float = 11,
    measured: dict[str, str] | None = None,
) -> Path:
    """Two-pass EBU R128 loudness normalization of the audio track; video is copied."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if measured is None:
        measured = measure_loudness(src, target_lufs=target_lufs, true_peak=true_peak, lra=lra)
    af = (
        f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}"
        f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured.get('target_offset', '0')}:linear=true:print_format=summary"
    )
    run_ffmpeg(
        ["-i", str(src), "-af", af, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(dst)],
        timeout=1800,
        description="loudnorm-apply",
    )
    return dst
