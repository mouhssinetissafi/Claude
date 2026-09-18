"""Branding: optional logo watermark.

If ``assets/branding/logo.png`` exists (and ``branding.watermark_enabled`` is
true) it is copied into the job work dir and described in ``timeline.json`` so
Remotion draws it. A missing, disabled or unreadable logo never fails a render:
the pipeline simply proceeds without a watermark and says so in the log.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from autoeditor.config import Config
from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.job import JobPaths

log = get_logger(__name__)

POSITIONS = ("top-right", "top-left", "bottom-right", "bottom-left")
DEFAULT_LOGO = "assets/branding/logo.png"


@dataclass
class WatermarkSpec:
    src: str  # relative to the job work dir
    position: str = "top-right"
    width_fraction: float = 0.14
    max_height_fraction: float = 0.08
    opacity: float = 0.85
    margin: int = 24

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def logo_path(cfg: Config) -> Path:
    raw = Path(str(cfg.get("branding.logo_path", DEFAULT_LOGO)))
    return raw if raw.is_absolute() else cfg.root / raw


def validate_logo(path: Path) -> str | None:
    """Return a reason the file cannot be used as a watermark, or None if it is fine."""
    if not path.exists() or not path.is_file():
        return "file does not exist"
    if path.stat().st_size == 0:
        return "file is empty"
    try:
        from PIL import Image  # type: ignore
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        return None
    try:
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
            if fmt != "PNG":
                return f"logo must be a PNG (got {fmt or 'unknown'})"
            if im.width < 16 or im.height < 16:
                return f"logo is too small ({im.width}x{im.height})"
    except OSError as exc:
        return f"logo is not a readable image: {exc}"
    return None


def resolve_watermark(paths: JobPaths, cfg: Config) -> tuple[dict[str, Any] | None, str | None]:
    """Return (watermark spec for timeline.json, warning). Both may be None."""
    if not bool(cfg.get("branding.watermark_enabled", True)):
        log.info("Watermark disabled in config (branding.watermark_enabled=false)")
        return None, None
    logo = logo_path(cfg)
    if not logo.exists():
        log.info("No logo at %s; rendering without a watermark", logo)
        return None, None
    problem = validate_logo(logo)
    if problem:
        warning = f"watermark skipped: {logo} - {problem}"
        log.warning(warning)
        return None, warning
    position = str(cfg.get("branding.position", "top-right")).lower()
    if position not in POSITIONS:
        log.warning("Unknown branding.position %r; using top-right", position)
        position = "top-right"
    dst = paths.work / "branding" / "logo.png"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(logo, dst)
    spec = WatermarkSpec(
        src=paths.rel(dst),
        position=position,
        width_fraction=max(0.04, min(0.3, float(cfg.get("branding.width_fraction", 0.14)))),
        max_height_fraction=max(0.03, min(0.2, float(cfg.get("branding.max_height_fraction", 0.08)))),
        opacity=max(0.05, min(1.0, float(cfg.get("branding.opacity", 0.85)))),
        margin=max(0, int(cfg.get("branding.margin", 24))),
    )
    log.info("Watermark: %s at %s (%.0f%% width, opacity %.2f)", logo.name, spec.position, spec.width_fraction * 100, spec.opacity)
    return spec.to_dict(), None
