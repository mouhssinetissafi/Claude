"""Configuration loading.

Defaults live in ``config/default.yaml``. A user config file (``--config``) is
deep-merged on top, then explicit CLI overrides. API keys are *never* part of the
config object; they are read from the environment by the providers.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"


class ConfigError(RuntimeError):
    """Raised when configuration cannot be loaded or is invalid."""


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass
class Config:
    """Thin wrapper over the merged config dict with dotted-path access."""

    data: dict[str, Any] = field(default_factory=dict)
    root: Path = REPO_ROOT
    mock: bool = False

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, path: str) -> Any:
        value = self.get(path, default=_MISSING)
        if value is _MISSING:
            raise ConfigError(f"Missing required config value: {path}")
        return value

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigError(f"Cannot set {path}: {part} is not a mapping")
        node[parts[-1]] = value

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError(f"Config section {name} is not a mapping")
        return value

    # Convenience path helpers -------------------------------------------------
    def path(self, key: str) -> Path:
        """Resolve a ``project.*_dir`` value relative to the repo root."""
        raw = self.require(f"project.{key}")
        p = Path(raw)
        return p if p.is_absolute() else self.root / p

    @property
    def work_dir(self) -> Path:
        return self.path("work_dir")

    @property
    def output_dir(self) -> Path:
        return self.path("output_dir")

    @property
    def cache_dir(self) -> Path:
        return self.path("cache_dir")

    @property
    def review_dir(self) -> Path:
        return self.path("review_dir")

    @property
    def assets_dir(self) -> Path:
        return self.path("assets_dir")

    @property
    def remotion_dir(self) -> Path:
        raw = Path(self.get("render.remotion_dir", "remotion"))
        return raw if raw.is_absolute() else self.root / raw


_MISSING = object()


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"Config file {path} must contain a mapping at top level")
    return loaded


def load_config(
    user_config: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    *,
    root: Path | None = None,
    mock: bool | None = None,
) -> Config:
    """Load default config, merge user config and overrides.

    ``overrides`` keys are dotted paths, e.g. ``{"script.target_max_seconds": 50}``.
    ``mock`` forces mock providers; defaults to ``AUTOEDITOR_MOCK=1`` in the env.
    """
    data = load_yaml(DEFAULT_CONFIG_PATH)
    if user_config is not None:
        data = _deep_merge(data, load_yaml(user_config))
    cfg = Config(data=data, root=root or REPO_ROOT)
    for key, value in (overrides or {}).items():
        if value is not None:
            cfg.set(key, value)
    if mock is None:
        mock = os.environ.get("AUTOEDITOR_MOCK", "").strip().lower() in {"1", "true", "yes"}
    cfg.mock = mock
    if mock:
        cfg.set("vision.provider", "mock")
        cfg.set("llm.provider", "mock")
        cfg.set("tts.provider", "mock")
        cfg.set("captions.provider", "mock")
    return cfg
