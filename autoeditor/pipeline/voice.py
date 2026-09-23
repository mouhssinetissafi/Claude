"""Phase 7: line-by-line TTS, exact durations, clean concatenation.

Outputs ``audio/line_001.<ext>``, ``audio/voice.mp3`` and ``voice_timing.json``.
Per-line audio is cached by text hash so re-runs only synthesize changed lines.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from autoeditor.cache import hash_text
from autoeditor.config import Config
from autoeditor.logging_utils import get_logger
from autoeditor.media import ffmpeg as ff
from autoeditor.media.ffprobe import audio_duration
from autoeditor.pipeline.job import JobPaths, read_json, write_json
from autoeditor.providers.base import TTSProvider
from autoeditor.schemas import validate

log = get_logger(__name__)


@dataclass
class LineTiming:
    line_id: int
    start: float
    end: float
    duration: float
    file: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _line_file(paths: JobPaths, line_id: int, ext: str) -> Path:
    return paths.audio_dir / f"line_{line_id:03d}{ext}"


def _sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".meta.json")


def synthesize_lines(script: dict[str, Any], paths: JobPaths, tts: TTSProvider, cfg: Config, *, force: bool = False) -> list[Path]:
    """Generate one audio file per narration line; reuse files whose text is unchanged."""
    files: list[Path] = []
    trim_db = cfg.get("tts.trim_silence_db")
    for line in script["lines"]:
        text = str(line["narration"]).strip()
        target = _line_file(paths, int(line["id"]), tts.output_extension)
        key = hash_text(f"{tts.name}|{text}")
        meta = _sidecar(target)
        reuse = not force and target.exists() and meta.exists() and read_json(meta).get("text_hash") == key
        if reuse:
            log.info("line %03d: reusing cached audio", line["id"])
        else:
            log.info("line %03d: synthesizing (%d chars)", line["id"], len(text))
            raw = tts.synthesize(text, target)
            if raw != target and raw.exists():
                raw.replace(target)
            if trim_db is not None and ff.ffmpeg_available() and tts.name != "mock":
                trimmed = target.with_name(target.stem + ".trim" + target.suffix)
                ff.trim_silence(target, trimmed, threshold_db=float(trim_db))
                trimmed.replace(target)
            write_json(meta, {"text_hash": key, "provider": tts.name})
        files.append(target)
    return files


def build_voice(script: dict[str, Any], paths: JobPaths, tts: TTSProvider, cfg: Config, *, force: bool = False) -> list[LineTiming]:
    """Synthesize, measure, concatenate and write voice.mp3 + voice_timing.json."""
    paths.audio_dir.mkdir(parents=True, exist_ok=True)
    files = synthesize_lines(script, paths, tts, cfg, force=force)
    gap = float(cfg.get("tts.inter_line_gap_seconds", 0.12))
    timings: list[LineTiming] = []
    cursor = 0.0
    for line, path in zip(script["lines"], files, strict=True):
        dur = audio_duration(path)
        if dur <= 0:
            raise RuntimeError(f"generated audio for line {line['id']} has zero duration")
        timings.append(LineTiming(line_id=int(line["id"]), start=round(cursor, 3), end=round(cursor + dur, 3), duration=round(dur, 3), file=paths.rel(path)))
        cursor += dur + gap
    ff.concat_audio(files, paths.voice_audio, gap_seconds=gap)
    total = audio_duration(paths.voice_audio)
    expected = timings[-1].end if timings else 0.0
    if abs(total - expected) > 0.5:
        log.warning("voice.mp3 is %.2fs but line timings sum to %.2fs", total, expected)
    payload = [t.to_dict() for t in timings]
    validate(payload, "voice_timing")
    write_json(paths.voice_timing_json, payload)
    log.info("voice.mp3 = %.2fs across %d lines", total, len(timings))
    return timings


def load_voice_timing(paths: JobPaths) -> list[LineTiming]:
    data = read_json(paths.voice_timing_json)
    validate(data, "voice_timing")
    return [LineTiming(**{k: item[k] for k in ("line_id", "start", "end", "duration", "file")}) for item in data]


def build_imported_voice(script: dict[str, Any], source: Path, paths: JobPaths, cfg: Config) -> list[LineTiming]:
    """Use a user-supplied narration file instead of TTS.

    The imported track is converted to the engine's canonical MP3 and line
    timings are estimated from each script line's share of the spoken words.
    Word-level captions may later refine timing with a local transcriber. The
    user's source file is never modified.
    """
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"imported narration not found: {source}")
    paths.audio_dir.mkdir(parents=True, exist_ok=True)
    ff.convert_audio(source, paths.voice_audio)
    total = audio_duration(paths.voice_audio)
    if total <= 0:
        raise RuntimeError("imported narration has zero duration")
    lines = list(script.get("lines", []))
    if not lines:
        raise RuntimeError("cannot align imported narration without script lines")
    import re

    counts = [max(1, len(re.findall(r"[A-Za-z0-9']+", str(line.get("narration", ""))))) for line in lines]
    total_words = sum(counts)
    cursor = 0.0
    timings: list[LineTiming] = []
    for index, (line, words) in enumerate(zip(lines, counts, strict=True)):
        # Put rounding residue on the final line so the full track is covered.
        duration = total - cursor if index == len(lines) - 1 else total * words / total_words
        start = cursor
        end = min(total, start + duration)
        timings.append(
            LineTiming(
                line_id=int(line["id"]),
                start=round(start, 3),
                end=round(end, 3),
                duration=round(end - start, 3),
                file=paths.rel(paths.voice_audio),
            )
        )
        cursor = end
    payload = [t.to_dict() for t in timings]
    validate(payload, "voice_timing")
    write_json(paths.voice_timing_json, payload)
    log.info("Imported narration: %.2fs across %d script lines", total, len(timings))
    return timings
