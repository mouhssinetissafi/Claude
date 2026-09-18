"""Content-addressed JSON cache for expensive operations (vision, LLM, TTS).

Keys are derived from *content*, never from file names alone, so a re-encoded or
renamed clip is re-analyzed while an unchanged one is served from cache.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

HASH_CHUNK = 1024 * 1024


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    return hash_bytes(text.encode("utf-8"))


def hash_file(path: Path, *, fast: bool = True) -> str:
    """Hash a file's content.

    With ``fast=True`` (default) large files are hashed by size + first/last
    4 MiB + middle 1 MiB, which is stable for identical files and cheap for
    multi-gigabyte footage. Full hashing is used for files <= 16 MiB.
    """
    size = path.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode())
    with path.open("rb") as fh:
        if not fast or size <= 16 * HASH_CHUNK:
            for chunk in iter(lambda: fh.read(HASH_CHUNK), b""):
                h.update(chunk)
        else:
            h.update(fh.read(4 * HASH_CHUNK))
            fh.seek(max(0, size // 2 - HASH_CHUNK // 2))
            h.update(fh.read(HASH_CHUNK))
            fh.seek(max(0, size - 4 * HASH_CHUNK))
            h.update(fh.read(4 * HASH_CHUNK))
    return h.hexdigest()


def stable_json(obj: Any) -> str:
    """Deterministic JSON serialization for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_key(namespace: str, *parts: Any) -> str:
    """Build a cache key from a namespace and arbitrary JSON-serializable parts."""
    payload = stable_json({"ns": namespace, "parts": list(parts)})
    return f"{namespace}_{hash_text(payload)[:40]}"


class JsonCache:
    """Directory-backed cache storing one JSON document per key."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in key)
        return self.directory / f"{safe}.json"

    def has(self, key: str) -> bool:
        return self._path(key).exists()

    def get(self, key: str) -> Any | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            # A corrupt cache entry must not break the run; treat as miss.
            return None

    def put(self, key: str, value: Any) -> None:
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
        tmp.replace(path)

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def clear(self) -> int:
        count = 0
        for p in self.directory.glob("*.json"):
            p.unlink()
            count += 1
        return count
