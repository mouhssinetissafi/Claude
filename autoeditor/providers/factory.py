"""Build the provider bundle from config (and mock mode)."""

from __future__ import annotations

from dataclasses import dataclass

from autoeditor.config import Config, ConfigError
from autoeditor.providers.base import LLMProvider, Transcriber, TTSProvider, VisionProvider


@dataclass
class Providers:
    llm: LLMProvider
    vision: VisionProvider
    tts: TTSProvider
    transcriber: Transcriber


def build_llm(cfg: Config) -> LLMProvider:
    name = str(cfg.get("llm.provider", "anthropic")).lower()
    if name == "mock":
        from autoeditor.providers.mock import MockLLM

        return MockLLM()
    if name == "anthropic":
        from autoeditor.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            model=str(cfg.get("llm.model", "claude-opus-5")),
            effort=str(cfg.get("llm.effort", "high")),
            max_json_retries=int(cfg.get("llm.max_json_retries", 2)),
            refusal_fallbacks=bool(cfg.get("llm.refusal_fallbacks", True)),
        )
    raise ConfigError(f"Unknown llm.provider: {name}")


def build_vision(cfg: Config) -> VisionProvider:
    name = str(cfg.get("vision.provider", "anthropic")).lower()
    if name == "mock":
        from autoeditor.providers.mock import MockVision

        return MockVision()
    if name == "anthropic":
        from autoeditor.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            model=str(cfg.get("vision.model", "claude-opus-5")),
            effort=str(cfg.get("vision.effort", "medium")),
            max_json_retries=int(cfg.get("llm.max_json_retries", 2)),
            refusal_fallbacks=bool(cfg.get("llm.refusal_fallbacks", True)),
        )
    raise ConfigError(f"Unknown vision.provider: {name}")


def build_tts(cfg: Config) -> TTSProvider:
    name = str(cfg.get("tts.provider", "elevenlabs")).lower()
    if name == "mock":
        from autoeditor.providers.mock import MockTTS

        return MockTTS(words_per_second=float(cfg.get("script.words_per_second", 2.6)))
    if name == "elevenlabs":
        from autoeditor.providers.elevenlabs_tts import ElevenLabsTTS

        return ElevenLabsTTS(
            voice_id=cfg.get("tts.voice_id"),
            model_id=str(cfg.get("tts.model_id", "eleven_multilingual_v2")),
            output_format=str(cfg.get("tts.output_format", "mp3_44100_128")),
            stability=float(cfg.get("tts.stability", 0.5)),
            similarity_boost=float(cfg.get("tts.similarity_boost", 0.75)),
        )
    raise ConfigError(f"Unknown tts.provider: {name}")


def build_transcriber(cfg: Config) -> Transcriber:
    name = str(cfg.get("captions.provider", "faster_whisper")).lower()
    if name == "mock":
        from autoeditor.providers.mock import MockTranscriber

        return MockTranscriber()
    if name in {"faster_whisper", "whisper"}:
        from autoeditor.providers.whisper_transcriber import FasterWhisperTranscriber

        return FasterWhisperTranscriber(
            model_size=str(cfg.get("captions.model_size", "small")),
            device=str(cfg.get("captions.device", "cpu")),
            compute_type=str(cfg.get("captions.compute_type", "int8")),
        )
    raise ConfigError(f"Unknown captions.provider: {name}")


def build_providers(cfg: Config, *, need_llm: bool = True, need_vision: bool = True, need_tts: bool = True, need_transcriber: bool = True) -> Providers:
    """Build only the providers a run needs, so skipped stages need no credentials."""
    from autoeditor.providers.mock import MockLLM, MockTranscriber, MockTTS, MockVision

    return Providers(
        llm=build_llm(cfg) if need_llm else MockLLM(),
        vision=build_vision(cfg) if need_vision else MockVision(),
        tts=build_tts(cfg) if need_tts else MockTTS(),
        transcriber=build_transcriber(cfg) if need_transcriber else MockTranscriber(),
    )
