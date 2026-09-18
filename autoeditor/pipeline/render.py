"""Phase 10: hand everything to the existing Remotion renderer.

The Python side never draws a frame. It stages the job's assets under
``remotion/public/jobs/<job>/`` (so ``staticFile()`` can serve them), writes a
props file with timeline + captions + script, and invokes ``npx remotion render``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from autoeditor.config import Config
from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.job import JobPaths, write_json

log = get_logger(__name__)


class RenderError(RuntimeError):
    pass


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            if dst.stat().st_ino == src.stat().st_ino or dst.stat().st_size == src.stat().st_size:
                return
        except OSError:
            pass
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def collect_asset_paths(timeline: dict[str, Any]) -> list[str]:
    srcs: list[str] = [timeline["voice"]]
    if timeline.get("music"):
        srcs.append(timeline["music"]["src"])
    for sfx in timeline.get("sfx", []):
        srcs.append(sfx["src"])
    if timeline.get("watermark"):
        srcs.append(timeline["watermark"]["src"])
    for line in timeline["lines"]:
        for seg in line["segments"]:
            srcs.append(seg["src"])
    seen: set[str] = set()
    ordered: list[str] = []
    for s in srcs:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


def stage_assets(paths: JobPaths, timeline: dict[str, Any], cfg: Config) -> str:
    """Copy/link every referenced asset into remotion/public/jobs/<job>/. Returns the asset base."""
    public_dir = cfg.remotion_dir / "public" / "jobs" / paths.name
    public_dir.mkdir(parents=True, exist_ok=True)
    for rel in collect_asset_paths(timeline):
        src = paths.work / rel
        if not src.exists():
            raise RenderError(f"timeline references missing asset: {rel}")
        _link_or_copy(src, public_dir / rel)
    return f"jobs/{paths.name}/"


def build_props(paths: JobPaths, timeline: dict[str, Any], captions: dict[str, Any], script: dict[str, Any], cfg: Config) -> dict[str, Any]:
    return {
        "jobId": paths.name,
        "assetBase": f"jobs/{paths.name}/",
        "theme": str(script.get("theme") or cfg.get("render.theme", "default")),
        "timeline": timeline,
        "captions": captions,
        "script": {
            "title": script["title"],
            "lines": [
                {
                    "id": int(line["id"]),
                    "narration": line["narration"],
                    "overlay_text": line.get("overlay_text"),
                    "emphasis_words": line.get("emphasis_words", []),
                }
                for line in script["lines"]
            ],
        },
    }


def remotion_ready(cfg: Config) -> bool:
    return (cfg.remotion_dir / "node_modules" / "@remotion" / "cli").exists()


def render_video(paths: JobPaths, cfg: Config, props: dict[str, Any], *, output: Path | None = None) -> Path:
    """Run ``npx remotion render`` and return the rendered file path."""
    remotion_dir = cfg.remotion_dir
    if not remotion_ready(cfg):
        raise RenderError(f"Remotion dependencies missing: run `npm install` inside {remotion_dir}")
    out = output or paths.render_raw_mp4
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json(paths.render_props_json, props)
    entry = str(cfg.get("render.entry", "src/index.ts"))
    composition = str(cfg.get("render.composition", "Short"))
    cmd = [
        "npx",
        "remotion",
        "render",
        entry,
        composition,
        str(out.resolve()),
        f"--props={paths.render_props_json.resolve()}",
        "--codec=h264",
        "--pixel-format=yuv420p",
        "--log=error",
        "--overwrite",
    ]
    browser = os.environ.get("REMOTION_BROWSER") or str(cfg.get("render.browser_executable") or "")
    if browser:
        cmd.append(f"--browser-executable={browser}")
    concurrency = int(cfg.get("render.concurrency", 0) or 0)
    if concurrency > 0:
        cmd.append(f"--concurrency={concurrency}")
    timeout = int(cfg.get("render.timeout_seconds", 1800))
    log.info("Rendering with Remotion: %s", " ".join(cmd[:6]) + " ...")
    try:
        proc = subprocess.run(cmd, cwd=str(remotion_dir), capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"Remotion render timed out after {timeout}s") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-25:])
        raise RenderError(f"Remotion render failed (exit {proc.returncode}):\n{tail}")
    if not out.exists() or out.stat().st_size == 0:
        raise RenderError("Remotion reported success but no output file was produced")
    log.info("Rendered %s (%.1f MB)", out.name, out.stat().st_size / 1e6)
    return out


def props_summary(props: dict[str, Any]) -> str:
    tl = props["timeline"]
    return json.dumps(
        {
            "duration": tl["duration"],
            "lines": len(tl["lines"]),
            "segments": sum(len(line["segments"]) for line in tl["lines"]),
            "captions_words": len(props["captions"]["words"]),
            "theme": props["theme"],
        }
    )
