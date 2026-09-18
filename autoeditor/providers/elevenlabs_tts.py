"""ElevenLabs text-to-speech via the public REST API.

Uses only the standard library so the dependency footprint stays small. The key
comes from ``ELEVENLABS_API_KEY``; the voice from ``ELEVENLABS_VOICE_ID`` or
config. The key is sent only as a request header and never logged.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from autoeditor.logging_utils import get_logger
from autoeditor.providers.base import MissingCredentialsError, ProviderError

log = get_logger(__name__)

API_BASE = "https://api.elevenlabs.io/v1"


class ElevenLabsTTS:
    name = "elevenlabs"
    output_extension = ".mp3"

    def __init__(
        self,
        *,
        voice_id: str | None = None,
        model_id: str = "eleven_multilingual_v2",
        output_format: str = "mp3_44100_128",
        stability: float = 0.5,
        similarity_boost: float = 0.75,
        max_retries: int = 3,
        timeout: float = 120.0,
    ) -> None:
        api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            raise MissingCredentialsError("ELEVENLABS_API_KEY is not set (or use --mock / tts.provider=mock)")
        self._api_key = api_key
        self.voice_id = (voice_id or os.environ.get("ELEVENLABS_VOICE_ID", "")).strip()
        if not self.voice_id:
            raise MissingCredentialsError("ELEVENLABS_VOICE_ID is not set and no tts.voice_id configured")
        self.model_id = model_id
        self.output_format = output_format
        self.stability = stability
        self.similarity_boost = similarity_boost
        self.max_retries = max_retries
        self.timeout = timeout

    def synthesize(self, text: str, dst: Path) -> Path:
        dst = dst.with_suffix(self.output_extension)
        dst.parent.mkdir(parents=True, exist_ok=True)
        url = f"{API_BASE}/text-to-speech/{self.voice_id}?output_format={self.output_format}"
        body = json.dumps(
            {
                "text": text,
                "model_id": self.model_id,
                "voice_settings": {"stability": self.stability, "similarity_boost": self.similarity_boost},
            }
        ).encode("utf-8")
        headers = {"xi-api-key": self._api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"}
        delay = 2.0
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    audio = resp.read()
                if not audio:
                    raise ProviderError("ElevenLabs returned an empty audio body")
                dst.write_bytes(audio)
                return dst
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (401, 403):
                    raise MissingCredentialsError("ElevenLabs rejected the API key") from exc
                if exc.code == 429 or exc.code >= 500:
                    log.warning("ElevenLabs HTTP %s on attempt %d; retrying in %.0fs", exc.code, attempt, delay)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise ProviderError(f"ElevenLabs HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                last = exc
                log.warning("ElevenLabs network error on attempt %d; retrying in %.0fs", attempt, delay)
                time.sleep(delay)
                delay *= 2
        raise ProviderError(f"ElevenLabs failed after {self.max_retries} attempts: {last}")
