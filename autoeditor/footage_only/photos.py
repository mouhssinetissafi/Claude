"""Native photo support for footage-only jobs.

A photograph has no duration, so the editor turns each one into a few
*framings*: slow camera moves over the picture (a pan or push on the whole
photo, a push-in on its most detailed region, a zoom-out reveal). A framing
behaves like a short clip in the timeline: it has a hold budget in seconds, it
plays at 1x along its camera path and it is never stretched or restarted to
fill time. The original file is never modified; a normalized copy (EXIF
orientation applied, metadata stripped, bounded size) lives in ``work/``.

Everything here is pure Pillow arithmetic so it can be unit-tested without
ffmpeg or a renderer. The renderer (Remotion) receives the camera path as a
``motion`` object on the timeline segment and interpolates it per frame.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from autoeditor.config import Config
from autoeditor.logging_utils import get_logger

log = get_logger(__name__)

PHOTO_KIND = "image"
VIDEO_KIND = "video"
DEFAULT_PHOTO_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"]
MAX_FRAMINGS = 3
# A photo segment may run this far past its hold budget (outro tail, sliver absorption)
# before the builder has to cut to something else; the camera path simply comes to rest.
PHOTO_GRACE_SECONDS = 1.0

FRAMING_PAN = "pan"
FRAMING_PUSH = "push"
FRAMING_DETAIL = "detail"
FRAMING_REVEAL = "reveal"

FRAMING_LABELS = {
    FRAMING_PAN: "slow pan across the photo",
    FRAMING_PUSH: "slow push on the whole photo",
    FRAMING_DETAIL: "slow push-in on a detail of the photo",
    FRAMING_REVEAL: "zoom-out reveal from a detail to the whole photo",
}


class PhotoError(RuntimeError):
    """A photo could not be read or converted."""


@dataclass
class PhotoInfo:
    filename: str
    path: str
    width: int
    height: int
    format: str
    mode: str
    orientation_applied: bool = False

    @property
    def aspect_ratio(self) -> float:
        return round(self.width / self.height, 4) if self.height else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "aspect_ratio": self.aspect_ratio, "kind": PHOTO_KIND}


# --------------------------------------------------------------------------- #
# Discovery helpers
# --------------------------------------------------------------------------- #
def photo_extensions(cfg: Config) -> set[str]:
    return {str(e).lower() for e in cfg.get("media.photo_extensions", DEFAULT_PHOTO_EXTENSIONS)}


def photo_hold_seconds(cfg: Config) -> float:
    return max(1.0, float(cfg.get("media.photo_hold_seconds", 4.0)))


def photo_framing_count(cfg: Config) -> int:
    return max(1, min(MAX_FRAMINGS, int(cfg.get("media.photo_framings", MAX_FRAMINGS))))


def photo_budget_seconds(cfg: Config) -> float:
    """Screen time one photo contributes to the footage budget (framings x hold)."""
    return round(photo_framing_count(cfg) * photo_hold_seconds(cfg), 3)


# --------------------------------------------------------------------------- #
# Reading + normalizing (Pillow)
# --------------------------------------------------------------------------- #
def _open(path: Path) -> Any:
    try:
        from PIL import Image, ImageOps  # type: ignore
    except ImportError as exc:  # pragma: no cover - Pillow is a hard dependency
        raise PhotoError("Pillow is required for photo support (pip install Pillow)") from exc
    try:
        im = Image.open(path)
        im.load()
    except (OSError, ValueError) as exc:
        raise PhotoError(f"cannot decode {path.name}: {exc}") from exc
    try:
        orientation = int(im.getexif().get(0x0112, 1) or 1)
    except (OSError, ValueError, TypeError, AttributeError):
        orientation = 1
    rotated = orientation in {2, 3, 4, 5, 6, 7, 8}
    if not rotated:
        return im, False
    try:
        transposed = ImageOps.exif_transpose(im)
    except (OSError, ValueError, TypeError):
        return im, False
    if transposed is None:  # pragma: no cover - older Pillow returned None for no-op
        return im, False
    im.close()
    return transposed, True


def probe_photo(path: Path) -> PhotoInfo:
    """Read dimensions with EXIF orientation applied; never writes anything."""
    if not path.exists():
        raise PhotoError(f"file does not exist: {path}")
    im, rotated = _open(path)
    with im:
        width, height = im.size
        if width <= 0 or height <= 0:
            raise PhotoError(f"invalid dimensions {width}x{height} in {path.name}")
        return PhotoInfo(
            filename=path.name,
            path=str(path),
            width=int(width),
            height=int(height),
            format=str(getattr(im, "format", None) or path.suffix.lstrip(".").upper() or "unknown"),
            mode=str(im.mode),
            orientation_applied=bool(rotated),
        )


def validate_photo(info: PhotoInfo, cfg: Config) -> str | None:
    """Return a rejection reason or None when the photo passes validation."""
    min_w = int(cfg.get("media.min_width", 480))
    min_h = int(cfg.get("media.min_height", 480))
    short_side = min(info.width, info.height)
    if short_side < min(min_w, min_h) or max(info.width, info.height) < max(min_w, min_h):
        return f"resolution too low ({info.width}x{info.height})"
    return None


def normalize_photo(src: Path, dst: Path, *, max_edge: int = 3840, quality: int = 92) -> PhotoInfo:
    """Write an oriented, metadata-free JPEG copy of ``src`` (downscaled, never upscaled)."""
    im, rotated = _open(src)
    with im:
        rgb = im.convert("RGB") if im.mode not in {"RGB"} else im
        width, height = rgb.size
        longest = max(width, height)
        if max_edge > 0 and longest > max_edge:
            ratio = max_edge / longest
            rgb = rgb.resize((max(1, round(width * ratio)), max(1, round(height * ratio))))
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            # No `exif=` argument: GPS/device metadata from the original is deliberately dropped.
            rgb.save(dst, format="JPEG", quality=int(quality), optimize=True, subsampling=0)
        except OSError as exc:
            raise PhotoError(f"could not write {dst.name}: {exc}") from exc
        return PhotoInfo(
            filename=dst.name,
            path=str(dst),
            width=int(rgb.size[0]),
            height=int(rgb.size[1]),
            format="JPEG",
            mode="RGB",
            orientation_applied=bool(rotated),
        )


# --------------------------------------------------------------------------- #
# Where is the interesting part of the picture?
# --------------------------------------------------------------------------- #
def detail_focus(path: Path, *, grid: int = 6) -> tuple[float, float]:
    """Centre (as image fractions) of the most detailed grid cell, with a mild centre bias.

    Uses edge energy on a small grayscale copy, so a textured subject beats flat sky
    or a plain backdrop. Falls back to the centre when the image cannot be read.
    """
    try:
        from PIL import ImageFilter  # type: ignore

        im, _ = _open(path)
    except (PhotoError, ImportError):
        return 0.5, 0.5
    with im:
        gray = im.convert("L")
        gray.thumbnail((256, 256))
        edges = gray.filter(ImageFilter.FIND_EDGES)
        w, h = edges.size
        if w < grid or h < grid:
            return 0.5, 0.5
        px = edges.load()
        best = (0.5, 0.5)
        best_score = -1.0
        for gy in range(grid):
            for gx in range(grid):
                x0, x1 = gx * w // grid, (gx + 1) * w // grid
                y0, y1 = gy * h // grid, (gy + 1) * h // grid
                total = 0
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        total += px[x, y]
                area = max(1, (x1 - x0) * (y1 - y0))
                cx = (x0 + x1) / (2 * w)
                cy = (y0 + y1) / (2 * h)
                # Cells at the very border are usually background; nudge toward the middle.
                bias = 1.0 - 0.35 * (abs(cx - 0.5) + abs(cy - 0.5))
                score = (total / area) * bias
                if score > best_score:
                    best_score = score
                    best = (round(cx, 4), round(cy, 4))
        return best


# --------------------------------------------------------------------------- #
# Camera paths ("framings")
# --------------------------------------------------------------------------- #
def cover_scale(width: int, height: int, frame_width: int, frame_height: int) -> float:
    """Scale that makes the photo cover the frame (the renderer's base scale)."""
    return max(frame_width / width, frame_height / height)


def clamp_focus(x: float, y: float, *, width: int, height: int, frame_width: int, frame_height: int, scale: float) -> tuple[float, float]:
    """Keep the focal point where the visible window still lies inside the photo (no bare edges)."""
    base = cover_scale(width, height, frame_width, frame_height)
    hx = min(0.5, frame_width / (2 * width * base * scale))
    hy = min(0.5, frame_height / (2 * height * base * scale))
    return (round(min(max(x, hx), 1 - hx), 4), round(min(max(y, hy), 1 - hy), 4))


def visible_box(state: dict[str, float], *, width: int, height: int, frame_width: int, frame_height: int) -> tuple[int, int, int, int]:
    """Source-pixel box (left, top, right, bottom) the frame shows for a camera state."""
    base = cover_scale(width, height, frame_width, frame_height)
    scale = float(state["scale"])
    vw = frame_width / (base * scale)
    vh = frame_height / (base * scale)
    left = float(state["x"]) * width - vw / 2
    top = float(state["y"]) * height - vh / 2
    left = min(max(0.0, left), max(0.0, width - vw))
    top = min(max(0.0, top), max(0.0, height - vh))
    return (int(round(left)), int(round(top)), int(round(min(width, left + vw))), int(round(min(height, top + vh))))


def _state(scale: float, x: float, y: float, dims: dict[str, int]) -> dict[str, float]:
    cx, cy = clamp_focus(x, y, scale=scale, **dims)
    return {"scale": round(scale, 4), "x": cx, "y": cy}


def plan_framings(
    width: int,
    height: int,
    *,
    frame_width: int,
    frame_height: int,
    index: int,
    hold: float,
    focus: tuple[float, float],
    count: int = MAX_FRAMINGS,
    push_zoom: float = 0.10,
    detail_zoom: float = 1.45,
) -> list[dict[str, Any]]:
    """Camera paths for one photo, most useful first (pure).

    * photos noticeably wider than the frame's aspect (landscape, square, 3:4 in a 9:16
      frame): a slow pan along the width with a whisper of zoom; photos close to the
      frame's aspect: a slow push (in, or out on odd photos)
    * a detail push-in on the most textured region
    * a zoom-out reveal from that detail to the whole picture

    Every focal point is clamped so the frame never shows a bare edge, at both ends of
    the move. ``index`` alternates directions so consecutive photos do not all drift the
    same way.
    """
    dims = {"width": width, "height": height, "frame_width": frame_width, "frame_height": frame_height}
    fx, fy = focus
    frame_aspect = frame_width / frame_height
    photo_aspect = width / height
    wider_than_frame = photo_aspect >= frame_aspect * 1.25
    forward = index % 2 == 0
    common = {
        "fit": "cover",
        "src_width": int(width),
        "src_height": int(height),
        "hold_seconds": round(float(hold), 3),
        "ease": "inout",
    }
    framings: list[dict[str, Any]] = []

    if wider_than_frame:
        z0, z1 = 1.0, 1.0 + push_zoom / 2
        # Travel across the middle 60% of the available horizontal room.
        base = cover_scale(width, height, frame_width, frame_height)
        hx = min(0.5, frame_width / (2 * width * base * z0))
        travel = max(0.0, 1 - 2 * hx)
        xa, xb = hx + 0.2 * travel, 1 - hx - 0.2 * travel
        y = 0.5 + 0.25 * (fy - 0.5)
        start = _state(z0, xa if forward else xb, y, dims)
        end = _state(z1, xb if forward else xa, y, dims)
        framings.append({**common, "framing": FRAMING_PAN, "primary": True, "from": start, "to": end})
    else:
        cx, cy = 0.5 + 0.35 * (fx - 0.5), 0.5 + 0.35 * (fy - 0.5)
        near, far = 1.0, 1.0 + push_zoom
        z_from, z_to = (near, far) if forward else (far, near)
        framings.append({**common, "framing": FRAMING_PUSH, "primary": True, "from": _state(z_from, cx, cy, dims), "to": _state(z_to, cx, cy, dims)})

    drift = 0.015 if forward else -0.015
    framings.append(
        {
            **common,
            "framing": FRAMING_DETAIL,
            "primary": False,
            "from": _state(detail_zoom * 0.93, fx - drift, fy, dims),
            "to": _state(detail_zoom, fx + drift, fy, dims),
        }
    )
    framings.append(
        {
            **common,
            "framing": FRAMING_REVEAL,
            "primary": False,
            "from": _state(detail_zoom, fx, fy, dims),
            "to": _state(1.0 + push_zoom * 0.3, 0.5, 0.5, dims),
        }
    )
    return framings[: max(1, min(MAX_FRAMINGS, count))]


def _lerp(a: float, b: float, t: float) -> float:
    return round(a + (b - a) * t, 4)


def slice_motion(motion: dict[str, Any], start: float, end: float, hold: float) -> dict[str, Any]:
    """The part of a camera path between ``start`` and ``end`` seconds of its hold.

    A segment that covers the whole path eases in and out; a partial slice moves
    linearly so consecutive slices of one path join without a hitch. Time past the
    hold budget rests at the end state (the picture simply holds).
    """
    hold = max(hold, 1e-6)
    p0 = min(1.0, max(0.0, start / hold))
    p1 = min(1.0, max(0.0, end / hold))
    if p1 < p0:
        p1 = p0
    src, dst = motion["from"], motion["to"]
    sliced = dict(motion)
    sliced["from"] = {k: _lerp(float(src[k]), float(dst[k]), p0) for k in ("scale", "x", "y")}
    sliced["to"] = {k: _lerp(float(src[k]), float(dst[k]), p1) for k in ("scale", "x", "y")}
    sliced["ease"] = "inout" if p0 <= 1e-6 and p1 >= 1 - 1e-6 else "linear"
    return sliced


def midpoint_state(motion: dict[str, Any]) -> dict[str, float]:
    return {k: _lerp(float(motion["from"][k]), float(motion["to"][k]), 0.5) for k in ("scale", "x", "y")}


# --------------------------------------------------------------------------- #
# Frames for vision / thumbnails
# --------------------------------------------------------------------------- #
def render_frame(photo: Path, dst: Path, *, motion: dict[str, Any] | None, frame_width: int, frame_height: int, width: int = 768, quality: int = 3) -> Path:
    """Write a JPEG of what the frame will show mid-move (whole photo for primary framings)."""
    im, _ = _open(photo)
    with im:
        rgb = im.convert("RGB")
        if motion is not None and not motion.get("primary", False):
            box = visible_box(midpoint_state(motion), width=rgb.size[0], height=rgb.size[1], frame_width=frame_width, frame_height=frame_height)
            if box[2] - box[0] > 8 and box[3] - box[1] > 8:
                rgb = rgb.crop(box)
        w, h = rgb.size
        if w > width:
            rgb = rgb.resize((width, max(1, round(h * width / w))))
        dst.parent.mkdir(parents=True, exist_ok=True)
        # ffmpeg's -q:v 2..31 maps roughly onto JPEG quality; keep the same knob semantics.
        jpeg_quality = int(max(50, min(95, 98 - quality * 3)))
        rgb.save(dst, format="JPEG", quality=jpeg_quality)
    return dst
