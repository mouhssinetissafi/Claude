"""Phase 11: automatic QC and finalization.

``finalize_output`` applies loudness normalization exactly once while moving the
raw render into ``output/<job>/final.mp4``. ``run_qc`` then inspects the final
file with ffprobe/ffmpeg and writes ``qc.json``. Pure evaluation helpers are
separated so they can be tested on synthetic measurements.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from autoeditor.config import Config
from autoeditor.logging_utils import get_logger
from autoeditor.media import ffmpeg as ff
from autoeditor.media.ffprobe import MediaInfo, ProbeError, probe
from autoeditor.pipeline.job import JobPaths, write_json
from autoeditor.schemas import validate

log = get_logger(__name__)


def finalize_output(paths: JobPaths, cfg: Config, *, raw: Path | None = None) -> Path:
    """Move the raw render to final.mp4, normalizing loudness once if enabled."""
    src = raw or paths.render_raw_mp4
    if not src.exists():
        raise FileNotFoundError(f"raw render missing: {src}")
    paths.output.mkdir(parents=True, exist_ok=True)
    if bool(cfg.get("audio.normalize_loudness", True)):
        log.info("Normalizing loudness to %s LUFS (single pass over final mix)", cfg.get("audio.loudness_target_lufs", -14))
        ff.normalize_loudness(
            src,
            paths.final_mp4,
            target_lufs=float(cfg.get("audio.loudness_target_lufs", -14)),
            true_peak=float(cfg.get("audio.loudness_true_peak_dbtp", -1.0)),
            lra=float(cfg.get("audio.loudness_range_lu", 11)),
        )
    else:
        shutil.copy2(src, paths.final_mp4)
    return paths.final_mp4


def _check(passed: bool, value: Any = None, expected: Any = None, message: str = "") -> dict[str, Any]:
    return {"passed": bool(passed), "value": value, "expected": expected, "message": message}


def evaluate_measurements(
    *,
    info: MediaInfo | None,
    expected_duration: float,
    black: list[ff.Interval],
    silence: list[ff.Interval],
    volume: dict[str, float],
    captions_end: float,
    narration_end: float,
    cfg: Config,
) -> dict[str, Any]:
    """Turn raw measurements into the qc.json document (pure function)."""
    checks: dict[str, Any] = {}
    warnings: list[str] = []
    width = int(cfg.get("video.width", 1080))
    height = int(cfg.get("video.height", 1920))
    min_d = float(cfg.get("qc.min_duration_seconds", 20))
    max_d = float(cfg.get("qc.max_duration_seconds", 62))
    tolerance = float(cfg.get("qc.caption_overrun_tolerance_seconds", 0.35))

    checks["output_exists"] = _check(info is not None, message="" if info else "final.mp4 missing or unreadable")
    if info is None:
        return {"passed": False, "checks": checks, "warnings": warnings}

    checks["video_stream"] = _check(info.has_video, info.codec, "h264")
    checks["audio_stream"] = _check(info.has_audio, info.audio_codec, "aac")
    checks["resolution"] = _check(info.width == width and info.height == height, f"{info.width}x{info.height}", f"{width}x{height}")
    dur_ok = min_d <= info.duration <= max_d and abs(info.duration - expected_duration) <= max(1.0, expected_duration * 0.05)
    checks["duration"] = _check(dur_ok, info.duration, {"expected": round(expected_duration, 2), "min": min_d, "max": max_d})

    long_black = [b for b in black if b.duration >= float(cfg.get("qc.black_min_seconds", 1.0))]
    checks["no_long_black"] = _check(not long_black, [[round(b.start, 2), round(b.end, 2)] for b in long_black], "none")

    sil_min = float(cfg.get("qc.silence_min_seconds", 2.0))
    # Ignore a short silent tail after the narration ends (outro breathing room).
    long_silence = [s for s in silence if s.duration >= sil_min and s.start < narration_end - 0.5]
    checks["no_long_silence"] = _check(not long_silence, [[round(s.start, 2), round(s.end, 2)] for s in long_silence], "none")
    if silence and not long_silence:
        warnings.append(f"{len(silence)} short/tail silence interval(s) detected")

    peak = volume.get("max_volume")
    max_peak = float(cfg.get("qc.max_peak_dbfs", -0.5))
    checks["audio_peak_safe"] = _check(peak is not None and peak <= max_peak, peak, f"<= {max_peak} dBFS")
    if peak is not None and peak < -12:
        warnings.append(f"audio peak is low ({peak} dBFS)")

    checks["captions_within_narration"] = _check(captions_end <= narration_end + tolerance, round(captions_end, 3), f"<= {round(narration_end + tolerance, 3)}")
    passed = all(c["passed"] for c in checks.values())
    return {"passed": passed, "checks": checks, "warnings": warnings}


def run_qc(paths: JobPaths, cfg: Config, *, expected_duration: float, captions: dict[str, Any], narration_end: float) -> dict[str, Any]:
    final = paths.final_mp4
    info: MediaInfo | None
    try:
        info = probe(final) if final.exists() else None
    except ProbeError as exc:
        log.error("ffprobe could not read final.mp4: %s", exc)
        info = None
    black: list[ff.Interval] = []
    silence: list[ff.Interval] = []
    volume: dict[str, float] = {}
    if info is not None:
        black = ff.detect_black(
            final, min_duration=float(cfg.get("qc.black_min_seconds", 1.0)), pixel_threshold=float(cfg.get("qc.black_pixel_threshold", 0.10))
        )
        if info.has_audio:
            silence = ff.detect_silence(final, noise_db=float(cfg.get("qc.silence_noise_db", -45)), min_duration=float(cfg.get("qc.silence_min_seconds", 2.0)))
            volume = ff.measure_volume(final)
    captions_end = max((w["end"] for w in captions.get("words", [])), default=0.0)
    result = evaluate_measurements(
        info=info,
        expected_duration=expected_duration,
        black=black,
        silence=silence,
        volume=volume,
        captions_end=captions_end,
        narration_end=narration_end,
        cfg=cfg,
    )
    result["output"] = str(final) if final.exists() else None
    validate(result, "qc")
    write_json(paths.qc_json, result)
    if result["passed"]:
        log.info("QC passed")
    else:
        failed = [k for k, v in result["checks"].items() if not v["passed"]]
        log.warning("QC FAILED: %s", ", ".join(failed))
    return result


def move_to_review(paths: JobPaths) -> Path:
    """Copy deliverables of a failed job to review/<job>/ for a human."""
    paths.review.mkdir(parents=True, exist_ok=True)
    for f in (paths.final_mp4, paths.qc_json, paths.metadata_json, paths.credits_txt, paths.thumbnail_jpg):
        if f.exists():
            shutil.copy2(f, paths.review / f.name)
    log.warning("Job needs human review: %s", paths.review)
    return paths.review
