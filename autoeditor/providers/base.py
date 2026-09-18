"""Provider protocols shared by real and mock implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """A provider failed in a way the pipeline cannot recover from."""


class MissingCredentialsError(ProviderError):
    """Required API credentials are not present in the environment."""


@dataclass
class LLMResult:
    data: dict[str, Any]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 1
    from_cache: bool = False


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        output_schema: dict[str, Any],
        max_tokens: int,
    ) -> LLMResult:
        """Return a JSON object matching ``output_schema``."""


@runtime_checkable
class VisionProvider(Protocol):
    name: str

    def analyze_images(
        self,
        *,
        system: str,
        user: str,
        images: list[Path],
        output_schema: dict[str, Any],
        max_tokens: int,
    ) -> LLMResult:
        """Return a JSON object describing the given images."""


@runtime_checkable
class TTSProvider(Protocol):
    name: str
    output_extension: str

    def synthesize(self, text: str, dst: Path) -> Path:
        """Write speech for ``text`` to ``dst`` (extension chosen by provider) and return the path."""


@dataclass
class TranscriptWord:
    text: str
    start: float
    end: float


@dataclass
class TranscriptSegment:
    text: str
    start: float
    end: float
    words: list[TranscriptWord] = field(default_factory=list)


@dataclass
class Transcript:
    language: str
    duration: float
    segments: list[TranscriptSegment] = field(default_factory=list)

    @property
    def words(self) -> list[TranscriptWord]:
        return [w for seg in self.segments for w in seg.words]


@runtime_checkable
class Transcriber(Protocol):
    name: str

    def transcribe(self, audio: Path, *, language: str, line_hints: list[dict[str, Any]] | None = None) -> Transcript:
        """Produce word-level timestamps for ``audio``.

        ``line_hints`` (``[{"line_id", "start", "end", "text"}]``) lets mock or
        alignment-based transcribers place words without acoustic analysis.
        """
