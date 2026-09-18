"""Shared fixtures. Synthetic clips are generated with ffmpeg when available.

All tests run in mock mode (AUTOEDITOR_MOCK=1) so no paid API is ever called.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from autoeditor.config import Config, load_config

os.environ["AUTOEDITOR_MOCK"] = "1"

FFMPEG = shutil.which("ffmpeg") is not None
requires_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Config rooted in a temp dir with small frames so encodes are fast."""
    c = load_config(root=tmp_path, mock=True)
    c.set("video.width", 270)
    c.set("video.height", 480)
    c.set("video.crf", 30)
    c.set("video.preset", "ultrafast")
    c.set("media.min_width", 100)
    c.set("media.min_height", 100)
    c.set("media.frame_width", 160)
    c.set("audio.music_enabled", False)
    c.set("audio.sfx_enabled", False)
    c.set("audio.normalize_loudness", False)
    # Short synthetic fixtures: relax the 45s Short policy (tested explicitly in test_duration.py).
    c.set("script.min_final_seconds", 6)
    c.set("script.target_min_seconds", 6)
    c.set("script.target_max_seconds", 20)
    c.set("script.hard_max_seconds", 30)
    c.set("script.min_footage_coverage", 0.5)
    c.set("qc.min_duration_seconds", 2)
    return c


def make_clip(path: Path, *, seconds: float = 4.0, size: str = "320x240", scenes: int = 2, audio: bool = True) -> Path:
    """Write a synthetic clip made of ``scenes`` solid-colour cuts."""
    colors = ["red", "blue", "green", "yellow", "magenta", "cyan"]
    per = seconds / max(1, scenes)
    inputs: list[str] = []
    labels = ""
    for i in range(scenes):
        inputs += ["-f", "lavfi", "-i", f"color=c={colors[i % len(colors)]}:s={size}:r=30:d={per:.3f}"]
        labels += f"[{i}:v]"
    filter_complex = f"{labels}concat=n={scenes}:v=1:a=0[v]"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds:.3f}"]
    cmd += ["-filter_complex", filter_complex, "-map", "[v]"]
    if audio:
        cmd += ["-map", f"{scenes}:a", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-t", f"{seconds:.3f}", str(path)]
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True)
    return path


def make_photo(
    path: Path,
    *,
    size: tuple[int, int] = (640, 480),
    detail_at: tuple[float, float] = (0.8, 0.8),
    orientation: int | None = None,
    fmt: str | None = None,
    gps: bool = False,
) -> Path:
    """Write a synthetic photograph: a smooth gradient with one high-detail patch.

    ``detail_at`` places a fine checkerboard (fractions of width/height) so the
    focus heuristic has something to find; ``orientation`` writes an EXIF
    orientation tag; ``gps`` adds a GPS block that normalization must strip.
    """
    from PIL import Image, ImageDraw

    w, h = size
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (int(40 + 120 * x / w), int(60 + 100 * y / h), 90)
    draw = ImageDraw.Draw(im)
    cx, cy = int(detail_at[0] * w), int(detail_at[1] * h)
    cell = 4
    for y in range(max(0, cy - 40), min(h, cy + 40), cell):
        for x in range(max(0, cx - 40), min(w, cx + 40), cell):
            if ((x // cell) + (y // cell)) % 2 == 0:
                draw.rectangle([x, y, x + cell - 1, y + cell - 1], fill=(250, 250, 250))
            else:
                draw.rectangle([x, y, x + cell - 1, y + cell - 1], fill=(5, 5, 5))
    path.parent.mkdir(parents=True, exist_ok=True)
    save_kwargs: dict[str, object] = {}
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    if gps:
        from PIL.TiffImagePlugin import IFDRational

        block = exif.get_ifd(0x8825)
        block[1] = "N"
        block[2] = (IFDRational(48, 1), IFDRational(51, 1), IFDRational(0, 1))
        block[3] = "E"
        block[4] = (IFDRational(2, 1), IFDRational(17, 1), IFDRational(0, 1))
    if orientation is not None or gps:
        save_kwargs["exif"] = exif.tobytes()
    im.save(path, format=fmt or None, **save_kwargs)  # type: ignore[arg-type]
    return path


@pytest.fixture
def photo_inbox(tmp_path: Path) -> Path:
    """An inbox with a photo-only job and a mixed photo/video job."""
    root = tmp_path / "inbox"
    only = root / "photos_only"
    make_photo(only / "car_road.jpg", size=(640, 480), detail_at=(0.7, 0.55), gps=True)
    make_photo(only / "wheel.jpg", size=(640, 480), detail_at=(0.3, 0.6))
    make_photo(only / "hood.jpg", size=(480, 640), detail_at=(0.5, 0.8), orientation=6)  # stored rotated
    make_photo(only / "badge.png", size=(500, 500), detail_at=(0.25, 0.25))
    make_photo(only / "seats.webp", size=(360, 640), detail_at=(0.5, 0.5))  # 9:16 like the frame
    make_photo(only / "tiny.jpg", size=(60, 40))  # rejected: resolution too low
    (only / "topic.txt").write_text("what makes this SUV look expensive\n", encoding="utf-8")
    (only / "car_road.license.json").write_text('{"source": "Press kit", "author": "Carmaker", "license": "Press use"}', encoding="utf-8")
    mixed = root / "mixed"
    if FFMPEG:
        make_clip(mixed / "drive.mp4", seconds=4.0, scenes=2)
        make_clip(mixed / "interior.mov", seconds=3.0, size="240x320", scenes=1, audio=False)
    make_photo(mixed / "front.jpg", size=(640, 480), detail_at=(0.6, 0.5))
    make_photo(mixed / "rear.jpg", size=(480, 640), detail_at=(0.5, 0.35))
    return root


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    """An inbox with one job that has a topic and one that does not."""
    if not FFMPEG:
        pytest.skip("ffmpeg required to build fixture clips")
    root = tmp_path / "inbox"
    job_a = root / "iphone_air"
    make_clip(job_a / "clip01.mp4", seconds=4.0, scenes=2)
    make_clip(job_a / "clip02.mov", seconds=5.0, size="240x320", scenes=2)
    (job_a / "topic.txt").write_text("why the iPhone Air is so thin\n", encoding="utf-8")
    (job_a / "clip01.license.json").write_text(
        '{"source": "Pexels", "author": "Test Author", "license": "Pexels License", "url": "https://example.com/v/1"}', encoding="utf-8"
    )
    (job_a / "notes.docx").write_bytes(b"not media")
    job_b = root / "no_topic"
    make_clip(job_b / "only.mp4", seconds=4.0, scenes=3, audio=False)
    (root / "empty_job").mkdir()
    (root / "empty_job" / "readme.txt").write_text("nothing here")
    return root
