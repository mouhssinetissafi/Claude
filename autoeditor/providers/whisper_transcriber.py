"""faster-whisper transcription with word timestamps.

``faster-whisper`` is an optional dependency (it pulls in CTranslate2). The
import happens lazily so the rest of the pipeline works without it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autoeditor.logging_utils import get_logger
from autoeditor.providers.base import ProviderError, Transcript, TranscriptSegment, TranscriptWord

log = get_logger(__name__)


class FasterWhisperTranscriber:
    name = "faster_whisper"

    def __init__(self, *, model_size: str = "small", device: str = "cpu", compute_type: str = "int8") -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise ProviderError("faster-whisper is not installed. `pip install faster-whisper` or set captions.provider=mock") from exc
        log.info("Loading faster-whisper model %s (%s/%s)", model_size, device, compute_type)
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, audio: Path, *, language: str, line_hints: list[dict[str, Any]] | None = None) -> Transcript:
        segments_iter, info = self._model.transcribe(str(audio), language=language or None, word_timestamps=True, vad_filter=True)
        segments: list[TranscriptSegment] = []
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        for seg in segments_iter:
            words = [
                TranscriptWord(text=w.word.strip(), start=round(float(w.start), 3), end=round(float(w.end), 3))
                for w in (seg.words or [])
                if w.word and w.word.strip()
            ]
            segments.append(TranscriptSegment(text=seg.text.strip(), start=round(float(seg.start), 3), end=round(float(seg.end), 3), words=words))
            duration = max(duration, float(seg.end))
        return Transcript(language=getattr(info, "language", language) or language, duration=round(duration, 3), segments=segments)
