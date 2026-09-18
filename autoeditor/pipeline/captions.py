"""Phase 8: word-level captions from the final joined narration.

The transcriber returns word timestamps; this module maps words to script
lines, flags emphasis words, groups words into short caption segments, clamps
everything to the narration duration and writes ``captions.json``.
"""

from __future__ import annotations

import re
from typing import Any

from autoeditor.config import Config
from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.job import JobPaths, write_json
from autoeditor.pipeline.voice import LineTiming
from autoeditor.providers.base import Transcriber, Transcript
from autoeditor.schemas import validate

log = get_logger(__name__)

_PUNCT = re.compile(r"[^\w']+", re.UNICODE)


def _norm(word: str) -> str:
    return _PUNCT.sub("", word).lower()


def _line_for(t: float, timings: list[LineTiming]) -> int | None:
    for lt in timings:
        if lt.start - 0.05 <= t <= lt.end + 0.05:
            return lt.line_id
    # Nearest line by midpoint if the word fell into a gap.
    if timings:
        nearest = min(timings, key=lambda lt: abs((lt.start + lt.end) / 2 - t))
        return nearest.line_id
    return None


def transcript_to_captions(
    transcript: Transcript,
    script: dict[str, Any],
    timings: list[LineTiming],
    *,
    voice_duration: float,
    max_words_per_caption: int = 4,
    language: str = "en",
) -> dict[str, Any]:
    emphasis_by_line: dict[int, set[str]] = {int(line["id"]): {_norm(w) for w in line.get("emphasis_words", []) if _norm(w)} for line in script["lines"]}
    words: list[dict[str, Any]] = []
    for tw in transcript.words:
        text = tw.text.strip()
        if not text:
            continue
        start = max(0.0, min(float(tw.start), voice_duration))
        end = max(start, min(float(tw.end), voice_duration))
        line_id = _line_for((start + end) / 2, timings)
        emphasis = bool(line_id is not None and _norm(text) in emphasis_by_line.get(line_id, set()))
        words.append({"text": text, "start": round(start, 3), "end": round(end, 3), "line_id": line_id, "emphasis": emphasis})

    segments: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_line: int | None = None
    for word in words:
        if current and (len(current) >= max_words_per_caption or word["line_id"] != current_line):
            segments.append(_segment(current))
            current = []
        current_line = word["line_id"]
        current.append(word)
    if current:
        segments.append(_segment(current))

    captions = {
        "version": 1,
        "language": transcript.language or language,
        "duration": round(voice_duration, 3),
        "words": words,
        "segments": segments,
    }
    validate(captions, "captions")
    return captions


def _segment(words: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "text": " ".join(w["text"] for w in words),
        "start": words[0]["start"],
        "end": words[-1]["end"],
        "words": list(words),
    }


def build_captions(
    script: dict[str, Any],
    timings: list[LineTiming],
    paths: JobPaths,
    transcriber: Transcriber,
    cfg: Config,
    *,
    voice_duration: float,
) -> dict[str, Any]:
    language = str(cfg.get("captions.language", "en"))
    narration_by_id = {int(line["id"]): str(line["narration"]) for line in script["lines"]}
    hints = [{"line_id": lt.line_id, "start": lt.start, "end": lt.end, "text": narration_by_id[lt.line_id]} for lt in timings]
    transcript = transcriber.transcribe(paths.voice_audio, language=language, line_hints=hints)
    if not transcript.words:
        raise RuntimeError("transcriber returned no words for voice.mp3")
    captions = transcript_to_captions(
        transcript,
        script,
        timings,
        voice_duration=voice_duration,
        max_words_per_caption=int(cfg.get("captions.max_words_per_caption", 4)),
        language=language,
    )
    write_json(paths.captions_json, captions)
    log.info("captions: %d words, %d segments", len(captions["words"]), len(captions["segments"]))
    return captions
