"""Normal mode timeline: one media asset per narration line.

Videos shorter than their slot loop (flagged) rather than stretch; images get a
Ken Burns move. Produces the same timeline.json contract as footage-only mode.
"""

from __future__ import annotations

from typing import Any

from autoeditor.config import Config
from autoeditor.footage_only.timeline import check_timeline_math
from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.job import JobPaths, write_json
from autoeditor.pipeline.media_fetch import MediaItem
from autoeditor.pipeline.voice import LineTiming
from autoeditor.schemas import validate

log = get_logger(__name__)


def build_normal_timeline(
    script: dict[str, Any],
    timings: list[LineTiming],
    media: list[MediaItem],
    paths: JobPaths,
    cfg: Config,
    *,
    voice_duration: float,
    music: dict[str, Any] | None = None,
    sfx: list[dict[str, Any]] | None = None,
    watermark: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_line = {m.line_id: m for m in media}
    timing_by_id = {t.line_id: t for t in timings}
    outro = float(cfg.get("timeline.outro_seconds", 0.6))
    total = round(max(voice_duration, timings[-1].end if timings else 0.0) + outro, 3)
    lines_out: list[dict[str, Any]] = []
    effect = "kenburns" if bool(cfg.get("timeline.ken_burns_images", True)) else "none"
    for idx, line in enumerate(script["lines"]):
        line_id = int(line["id"])
        timing = timing_by_id[line_id]
        item = by_line.get(line_id)
        if item is None:
            raise RuntimeError(f"no media resolved for line {line_id}")
        end = total if idx == len(script["lines"]) - 1 else timing.end
        length = round(end - timing.start, 3)
        if item.type == "image":
            seg = {
                "src": item.src,
                "type": "image",
                "start": timing.start,
                "end": end,
                "scene_id": None,
                "effect": effect,
                "transition": str(cfg.get("timeline.transition", "cut")),
                "fallback": None,
            }
        else:
            available = float(item.duration or 0.0)
            span = min(length, available) if available > 0 else length
            seg = {
                "src": item.src,
                "type": "video",
                "start": timing.start,
                "end": end,
                "source_start": 0.0,
                "source_end": round(span, 3),
                "scene_id": None,
                "effect": "none",
                "transition": str(cfg.get("timeline.transition", "cut")),
                "fallback": "loop" if span + 0.02 < length else None,
            }
            if seg["fallback"] == "loop":
                log.info("line %03d: media is %.1fs for a %.1fs slot; looping", line_id, available, length)
        lines_out.append(
            {
                "line_id": line_id,
                "start": timing.start,
                "end": end,
                "overlay_text": line.get("overlay_text"),
                "emphasis_words": line.get("emphasis_words", []),
                "segments": [seg],
            }
        )
    timeline = {
        "version": 1,
        "fps": int(cfg.get("video.fps", 30)),
        "width": int(cfg.get("video.width", 1080)),
        "height": int(cfg.get("video.height", 1920)),
        "duration": total,
        "voice": paths.rel(paths.voice_audio),
        "music": music,
        "sfx": sfx or [],
        "watermark": watermark,
        "lines": lines_out,
    }
    validate(timeline, "timeline")
    check_timeline_math(timeline, allow_loop=True)
    write_json(paths.timeline_json, timeline)
    return timeline
