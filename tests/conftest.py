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
    c.set("script.target_min_seconds", 6)
    c.set("script.target_max_seconds", 20)
    c.set("script.hard_max_seconds", 30)
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
