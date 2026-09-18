"""Logging helpers with secret redaction.

Anything that looks like an API key (``sk-ant-...``, ElevenLabs-style hex keys,
or the literal values of known secret env vars) is masked before it reaches a
handler, so provider errors can be logged safely.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

SECRET_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ELEVENLABS_API_KEY",
    "YOUTUBE_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN",
)

_KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|authorization|x-api-key|xi-api-key)([\"'\s:=]+)([A-Za-z0-9_\-\.]{12,})"),
]


def redact(text: str) -> str:
    """Mask secrets in ``text``."""
    if not text:
        return text
    for var in SECRET_ENV_VARS:
        value = os.environ.get(var)
        if value and len(value) >= 6:
            text = text.replace(value, f"<{var}:redacted>")
    text = _KEY_PATTERNS[0].sub("sk-ant-<redacted>", text)
    text = _KEY_PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    return text


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken log record must never crash the pipeline
            return True
        record.msg = redact(message)
        record.args = ()
        return True


_CONFIGURED = False


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Configure root logging once. Safe to call repeatedly."""
    global _CONFIGURED
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(fmt)
        handler.addFilter(SecretRedactingFilter())
        root.addHandler(handler)
        _CONFIGURED = True
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        already = any(isinstance(h, logging.FileHandler) and Path(h.baseFilename) == log_file for h in root.handlers)
        if not already:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            fh.addFilter(SecretRedactingFilter())
            root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
