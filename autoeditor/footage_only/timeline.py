"""Phase 9: timeline generation.

For every narration line the slot equals the measured TTS duration. Assigned
scenes fill the slot in order; when they run short the fallback chain is:

  1. a visually similar unused scene
  2. a different section of the same source clip
  3. tasteful reuse of a scene outside the repeat window
  4. a Ken Burns still frame of the best available scene

No slot is ever left empty and clips are never stretched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from autoeditor.config import Config
from autoeditor.footage_only.scenes import Scene
from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.job import JobPaths, write_json
from autoeditor.pipeline.voice import LineTiming
from autoeditor.schemas import validate

log = get_logger(__name__)

EPS = 0.02
# Shortest cut the builder will ever emit; anything smaller is absorbed by its neighbour.
SLIVER = 0.5


@dataclass
class SceneUsage:
    """Tracks which parts of each scene were used and when."""

    consumed: dict[int, float] = field(default_factory=dict)  # scene_id -> seconds already used
    last_used_at: dict[int, float] = field(default_factory=dict)  # scene_id -> timeline time of last use
    uses: dict[int, int] = field(default_factory=dict)

    def remaining(self, scene: Scene) -> float:
        return max(0.0, scene.duration - self.consumed.get(scene.scene_id, 0.0))

    def record(self, scene: Scene, seconds: float, at: float) -> None:
        self.consumed[scene.scene_id] = self.consumed.get(scene.scene_id, 0.0) + seconds
        self.last_used_at[scene.scene_id] = at
        self.uses[scene.scene_id] = self.uses.get(scene.scene_id, 0) + 1

    def used(self, scene_id: int) -> bool:
        return scene_id in self.uses


def _segment(scene: Scene, *, start: float, length: float, offset: float, transition: str, fallback: str | None) -> dict[str, Any]:
    return {
        "src": scene.normalized_file,
        "type": "video",
        "start": round(start, 3),
        "end": round(start + length, 3),
        "source_start": round(scene.start_time + offset, 3),
        "source_end": round(scene.start_time + offset + length, 3),
        "scene_id": scene.scene_id,
        "effect": "none",
        "transition": transition,
        "fallback": fallback,
    }


def _still_segment(scene: Scene, *, start: float, length: float, transition: str) -> dict[str, Any]:
    frame = scene.frames[0] if scene.frames else scene.normalized_file
    return {
        "src": frame,
        "type": "image" if scene.frames else "video",
        "start": round(start, 3),
        "end": round(start + length, 3),
        "source_start": round(scene.start_time, 3),
        "source_end": round(scene.start_time + min(length, scene.duration), 3),
        "scene_id": scene.scene_id,
        "effect": "kenburns",
        "transition": transition,
        "fallback": "still_frame",
    }


class TimelineBuilder:
    def __init__(self, scenes: list[Scene], inventory: dict[str, Any], cfg: Config) -> None:
        self.scenes = {s.scene_id: s for s in scenes if s.usable}
        self.inventory_rows = {int(r["scene_id"]): r for r in inventory.get("usable_scenes", [])}
        self.strongest = [int(s["scene_id"]) for s in inventory.get("strongest_scenes", [])]
        self.cfg = cfg
        self.usage = SceneUsage()
        self.min_seg = float(cfg.get("timeline.min_segment_seconds", 1.2))
        self.max_seg = float(cfg.get("timeline.max_segment_seconds", 4.5))
        self.repeat_window = float(cfg.get("timeline.repeat_window_seconds", 30))
        self.transition = str(cfg.get("timeline.transition", "cut"))
        self.allow_still = bool(cfg.get("timeline.allow_still_frame_fallback", True))
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ #
    def _score(self, scene_id: int) -> float:
        return float(self.inventory_rows.get(scene_id, {}).get("score", 0))

    def _recently_used(self, scene_id: int, at: float) -> bool:
        last = self.usage.last_used_at.get(scene_id)
        return last is not None and (at - last) < self.repeat_window

    def _acceptable(self, scene: Scene, need: float, floor: float) -> bool:
        """A scene can take a segment if it can finish the slot or make a proper-length cut."""
        remaining = self.usage.remaining(scene)
        return remaining >= need - EPS or remaining >= floor

    def _pick_fallback(self, *, anchors: list[int], at: float, need: float, exclude: set[int]) -> tuple[Scene | None, str]:
        """Apply the fallback chain; return (scene, reason)."""
        candidates = [s for sid, s in self.scenes.items() if sid not in exclude and self._acceptable(s, need, self.min_seg)]
        # 1. similar, unused scene
        similar_ids: list[int] = []
        for a in anchors:
            similar_ids.extend(int(x) for x in self.inventory_rows.get(a, {}).get("similar_to", []))
        for s in sorted(candidates, key=lambda s: -self._score(s.scene_id)):
            if s.scene_id in similar_ids and not self.usage.used(s.scene_id):
                return s, "similar_scene"
        # 2. different section of the same source clip (unused first)
        anchor_sources = {self.scenes[a].source_file for a in anchors if a in self.scenes}
        same_source = [s for s in candidates if s.source_file in anchor_sources]
        for s in sorted(same_source, key=lambda s: (self.usage.used(s.scene_id), -self._score(s.scene_id))):
            if not self._recently_used(s.scene_id, at):
                return s, "same_source_section"
        # 2b. any unused scene anywhere
        unused = [s for s in candidates if not self.usage.used(s.scene_id)]
        if unused:
            return max(unused, key=lambda s: self._score(s.scene_id)), "unused_scene"
        # 3. tasteful reuse outside the repeat window
        outside = [s for s in candidates if not self._recently_used(s.scene_id, at)]
        if outside:
            return max(outside, key=lambda s: self._score(s.scene_id)), "reuse_outside_window"
        # 3b. short unused remainders of any clip (still a different section, still real footage).
        leftovers = [s for sid, s in self.scenes.items() if sid not in exclude and self._acceptable(s, need, SLIVER) and self.usage.remaining(s) > EPS]
        if leftovers:
            return max(leftovers, key=lambda s: (self.usage.remaining(s), self._score(s.scene_id))), "same_source_section"
        # Reuse inside window as a last resort before stills (never black).
        if candidates:
            self.warnings.append(f"scene reused within {self.repeat_window:.0f}s at {at:.1f}s")
            return min(candidates, key=lambda s: self.usage.last_used_at.get(s.scene_id, -1)), "reuse_in_window"
        return None, "none"

    def _shrink_last(self, segments: list[dict[str, Any]], amount: float) -> float:
        """Take ``amount`` seconds back from the previous segment; returns what was reclaimed."""
        if not segments or amount <= 0:
            return 0.0
        last = segments[-1]
        length = last["end"] - last["start"]
        if length - amount < SLIVER:
            return 0.0
        last["end"] = round(last["end"] - amount, 3)
        if last["type"] == "video":
            last["source_end"] = round(last["source_end"] - amount, 3)
            scene_id = last.get("scene_id")
            if scene_id in self.usage.consumed:
                self.usage.consumed[scene_id] = max(0.0, self.usage.consumed[scene_id] - amount)
        return amount

    def _extend_last(self, segments: list[dict[str, Any]], extra: float, at: float) -> bool:
        """Extend the previous segment by ``extra`` seconds when its source allows it."""
        if not segments or extra <= 0:
            return False
        last = segments[-1]
        if last["type"] == "image":
            last["end"] = round(last["end"] + extra, 3)
            return True
        scene = self.scenes.get(last.get("scene_id") or -1)
        if scene is None or last["source_end"] + extra > scene.end_time + EPS:
            return False
        last["end"] = round(last["end"] + extra, 3)
        last["source_end"] = round(last["source_end"] + extra, 3)
        self.usage.record(scene, extra, at)
        return True

    # ------------------------------------------------------------------ #
    def fill_line(self, line: dict[str, Any], timing: LineTiming) -> list[dict[str, Any]]:
        slot = timing.duration
        cursor = timing.start
        end = timing.end
        assigned = [int(sid) for sid in line.get("scene_ids", []) if int(sid) in self.scenes]
        # Drop assignments that would produce too many cuts for the slot.
        max_cuts = max(1, int(slot // self.min_seg)) if slot >= self.min_seg else 1
        if len(assigned) > max_cuts:
            assigned = assigned[:max_cuts]
        segments: list[dict[str, Any]] = []
        queue = list(assigned)
        used_here: set[int] = set()

        while end - cursor > EPS:
            remaining_slot = end - cursor
            # Never emit a sliver: stretch the previous segment over a tiny remainder when possible,
            # otherwise borrow time from it so the closing cut has a proper length.
            if remaining_slot < SLIVER and segments:
                if self._extend_last(segments, remaining_slot, cursor):
                    cursor = end
                    break
                borrowed = self._shrink_last(segments, SLIVER - remaining_slot)
                if borrowed > 0:
                    cursor = round(cursor - borrowed, 3)
                    remaining_slot = end - cursor
            scene: Scene | None = None
            fallback: str | None = None
            while queue and scene is None:
                cand = self.scenes[queue.pop(0)]
                if self._acceptable(cand, remaining_slot, self.min_seg):
                    scene = cand
            if scene is None:
                scene, fallback = self._pick_fallback(anchors=assigned or list(used_here), at=cursor, need=remaining_slot, exclude=used_here)
            if scene is None:
                # 4. still frame of the best scene we know about (never leave black).
                anchor_id = (assigned or self.strongest or list(self.scenes))[0]
                still = self.scenes[anchor_id]
                if not self.allow_still:
                    self.warnings.append(f"no footage left for line {timing.line_id}; still frame used despite config")
                segments.append(_still_segment(still, start=cursor, length=remaining_slot, transition=self.transition))
                self.usage.record(still, 0.0, cursor)
                cursor = end
                break

            # How long should this segment be?
            planned_share = remaining_slot / (1 + len(queue)) if queue else remaining_slot
            length = min(remaining_slot, self.usage.remaining(scene), max(planned_share, self.min_seg))
            if remaining_slot - length < self.min_seg and remaining_slot - length > EPS and self.usage.remaining(scene) >= remaining_slot - EPS:
                length = remaining_slot  # avoid a tiny leftover cut
            # Strong footage may hold longer; ordinary footage is split at max_seg.
            if length > self.max_seg and scene.scene_id not in self.strongest and len(self.scenes) > 1:
                length = self.max_seg
            length = round(min(length, remaining_slot, self.usage.remaining(scene)), 3)
            if length <= EPS:
                # Candidate has nothing left (should not happen); fall through to a still frame.
                segments.append(_still_segment(scene, start=cursor, length=remaining_slot, transition=self.transition))
                cursor = end
                break
            offset = self.usage.consumed.get(scene.scene_id, 0.0)
            segments.append(_segment(scene, start=cursor, length=length, offset=offset, transition=self.transition, fallback=fallback))
            self.usage.record(scene, length, cursor)
            used_here.add(scene.scene_id)
            cursor = round(cursor + length, 3)

        # Snap the last segment to the slot end exactly.
        if segments:
            last = segments[-1]
            drift = round(end - last["end"], 3)
            if abs(drift) > 0:
                last["end"] = round(end, 3)
                if last["type"] == "video":
                    last["source_end"] = round(last["source_end"] + drift, 3)
        return segments


def build_timeline(
    script: dict[str, Any],
    timings: list[LineTiming],
    scenes: list[Scene],
    inventory: dict[str, Any],
    paths: JobPaths,
    cfg: Config,
    *,
    voice_duration: float,
    music: dict[str, Any] | None = None,
    sfx: list[dict[str, Any]] | None = None,
    watermark: dict[str, Any] | None = None,
) -> dict[str, Any]:
    builder = TimelineBuilder(scenes, inventory, cfg)
    if not builder.scenes:
        raise RuntimeError("no usable scenes available for the timeline")
    timing_by_id = {t.line_id: t for t in timings}
    lines_out: list[dict[str, Any]] = []
    for line in script["lines"]:
        timing = timing_by_id.get(int(line["id"]))
        if timing is None:
            raise RuntimeError(f"no voice timing for line {line['id']}")
        segments = builder.fill_line(line, timing)
        lines_out.append(
            {
                "line_id": int(line["id"]),
                "start": timing.start,
                "end": timing.end,
                "overlay_text": line.get("overlay_text"),
                "emphasis_words": line.get("emphasis_words", []),
                "segments": segments,
            }
        )

    outro = float(cfg.get("timeline.outro_seconds", 0.6))
    total = round(max(voice_duration, timings[-1].end if timings else 0.0) + outro, 3)
    # Extend the final segment through the outro so the tail is never black.
    if lines_out and lines_out[-1]["segments"]:
        segments = lines_out[-1]["segments"]
        last = segments[-1]
        extra = round(total - last["end"], 3)
        if extra > 0 and not builder._extend_last(segments, extra, last["end"]):
            scene = builder.scenes.get(last.get("scene_id") or -1)
            anchor = scene or builder.scenes[(builder.strongest or list(builder.scenes))[0]]
            segments.append(_still_segment(anchor, start=last["end"], length=extra, transition=builder.transition))
        lines_out[-1]["end"] = total

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
    check_timeline_math(timeline)
    write_json(paths.timeline_json, timeline)
    for w in builder.warnings:
        log.warning("timeline: %s", w)
    log.info("Timeline: %.2fs, %d segments", total, sum(len(line["segments"]) for line in lines_out))
    return timeline


def check_timeline_math(timeline: dict[str, Any], *, allow_loop: bool = False) -> None:
    """Assert segments tile each line exactly and never exceed the duration.

    Video segments must play at 1x: the source span equals the slot, unless the
    segment is explicitly a loop (normal mode) and ``allow_loop`` is set.
    """
    for line in timeline["lines"]:
        cursor = line["start"]
        for seg in line["segments"]:
            if abs(seg["start"] - cursor) > EPS:
                raise ValueError(f"line {line['line_id']}: gap/overlap at {cursor} (segment starts {seg['start']})")
            if seg["end"] <= seg["start"]:
                raise ValueError(f"line {line['line_id']}: empty segment at {seg['start']}")
            if seg["type"] == "video":
                slot = seg["end"] - seg["start"]
                span = seg["source_end"] - seg["source_start"]
                if span - slot > EPS * 2:
                    raise ValueError(f"line {line['line_id']}: source span longer than slot (clip would be sped up)")
                looped = allow_loop and seg.get("fallback") == "loop"
                if slot - span > EPS * 2 and not looped:
                    raise ValueError(f"line {line['line_id']}: slot longer than source span (clip would be stretched)")
            cursor = seg["end"]
        if abs(cursor - line["end"]) > EPS:
            raise ValueError(f"line {line['line_id']}: segments end at {cursor}, line ends at {line['end']}")
    if timeline["lines"] and timeline["lines"][-1]["end"] - timeline["duration"] > EPS:
        raise ValueError("timeline lines exceed total duration")


def repeat_violations(timeline: dict[str, Any], window: float) -> list[str]:
    """Scenes reused within ``window`` seconds (diagnostic used by tests/QC)."""
    last_seen: dict[int, float] = {}
    problems: list[str] = []
    for line in timeline["lines"]:
        for seg in line["segments"]:
            sid = seg.get("scene_id")
            if sid is None or seg.get("fallback") == "still_frame":
                continue
            if sid in last_seen and seg["start"] - last_seen[sid] < window and seg.get("fallback") not in {"reuse_outside_window"}:
                if seg.get("fallback") == "reuse_in_window":
                    problems.append(f"scene {sid} reused at {seg['start']} (within {window}s)")
            last_seen[sid] = seg["end"]
    return problems
