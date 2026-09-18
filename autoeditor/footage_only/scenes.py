"""Phase 3: scene detection and representative frames.

Uses PySceneDetect when available, else ffmpeg's ``scene`` filter. Scenes are
logical records (no physical splitting). Over-long scenes are chunked so the
timeline has enough variety, and very short scenes are merged into neighbors.

Photos become scenes too: each photo yields a few *framings* (slow camera
moves, see ``photos.py``) that carry a hold budget instead of a real duration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from autoeditor.cache import hash_text
from autoeditor.config import Config
from autoeditor.footage_only.normalize import SourceClip
from autoeditor.footage_only.photos import (
    PHOTO_KIND,
    VIDEO_KIND,
    PhotoError,
    detail_focus,
    photo_framing_count,
    photo_hold_seconds,
    plan_framings,
    render_frame,
)
from autoeditor.logging_utils import get_logger
from autoeditor.media import ffmpeg as ff
from autoeditor.pipeline.job import JobPaths, write_json
from autoeditor.schemas import validate

log = get_logger(__name__)


@dataclass
class Scene:
    scene_id: int
    source_file: str
    normalized_file: str
    start_time: float
    end_time: float
    duration: float
    frames: list[str] = field(default_factory=list)
    content_hash: str = ""
    analysis: dict[str, Any] | None = None
    rejected_reason: str | None = None
    kind: str = VIDEO_KIND  # video | image
    motion: dict[str, Any] | None = None  # photo framings only: the camera path (see photos.plan_framings)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def usable(self) -> bool:
        return self.rejected_reason is None

    @property
    def is_photo(self) -> bool:
        return self.kind == PHOTO_KIND

    @property
    def framing(self) -> str | None:
        return str(self.motion.get("framing")) if self.motion else None

    @property
    def primary_framing(self) -> bool:
        """The framing that represents the whole photo (the one vision analyzes)."""
        return bool(self.motion.get("primary", False)) if self.motion else False


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def _detect_with_pyscenedetect(video: Path, cfg: Config) -> list[tuple[float, float]] | None:
    try:
        from scenedetect import AdaptiveDetector, ContentDetector, SceneManager, open_video  # type: ignore
    except ImportError:
        return None
    threshold = float(cfg.get("media.scene_threshold", 27.0))
    min_scene = float(cfg.get("media.min_scene_seconds", 0.8))
    stream = open_video(str(video))
    fps = float(getattr(stream, "frame_rate", 30.0) or 30.0)
    manager = SceneManager()
    detector_name = str(cfg.get("media.scene_detector", "content")).lower()
    min_len = max(1, int(round(min_scene * fps)))
    if detector_name == "adaptive":
        manager.add_detector(AdaptiveDetector(min_scene_len=min_len))
    else:
        manager.add_detector(ContentDetector(threshold=threshold, min_scene_len=min_len))
    manager.detect_scenes(stream, show_progress=False)
    scene_list = manager.get_scene_list()
    return [(round(s.get_seconds(), 3), round(e.get_seconds(), 3)) for s, e in scene_list]


def _detect_with_ffmpeg(video: Path, duration: float, cfg: Config) -> list[tuple[float, float]]:
    cuts = ff.detect_scene_changes_ffmpeg(video, threshold=float(cfg.get("media.ffmpeg_scene_threshold", 0.4)))
    bounds = [0.0, *[c for c in cuts if 0.0 < c < duration], duration]
    return [(round(bounds[i], 3), round(bounds[i + 1], 3)) for i in range(len(bounds) - 1)]


def refine_scene_bounds(raw: list[tuple[float, float]], duration: float, *, min_scene: float, max_scene: float, max_scenes: int) -> list[tuple[float, float]]:
    """Merge too-short scenes into neighbors and chunk too-long ones (pure)."""
    if duration <= 0:
        return []
    bounds = [(max(0.0, s), min(duration, e)) for s, e in raw if e > s]
    if not bounds:
        bounds = [(0.0, duration)]
    # Merge short scenes forward into the next (or backward for the last one).
    merged: list[tuple[float, float]] = []
    for s, e in bounds:
        if merged and (e - s) < min_scene:
            ps, _ = merged[-1]
            merged[-1] = (ps, e)
        elif not merged and (e - s) < min_scene and len(bounds) > 1:
            # first scene is tiny: it will be absorbed by the next one
            merged.append((s, e))
            continue
        else:
            merged.append((s, e))
    # Re-check the first scene which may still be tiny.
    if len(merged) > 1 and merged[0][1] - merged[0][0] < min_scene:
        merged[1] = (merged[0][0], merged[1][1])
        merged.pop(0)
    # Chunk long scenes into pieces <= max_scene.
    chunked: list[tuple[float, float]] = []
    for s, e in merged:
        length = e - s
        if length <= max_scene:
            chunked.append((s, e))
            continue
        pieces = int(length // max_scene) + (1 if length % max_scene > min_scene else 0)
        pieces = max(1, pieces)
        step = length / pieces
        for i in range(pieces):
            chunked.append((round(s + i * step, 3), round(s + (i + 1) * step, 3) if i < pieces - 1 else e))
    if len(chunked) > max_scenes:
        # Keep evenly spread scenes to bound API cost.
        stride = len(chunked) / max_scenes
        chunked = [chunked[int(i * stride)] for i in range(max_scenes)]
    return [(round(s, 3), round(e, 3)) for s, e in chunked]


def detect_scenes(video: Path, duration: float, cfg: Config) -> list[tuple[float, float]]:
    detector = str(cfg.get("media.scene_detector", "content")).lower()
    raw: list[tuple[float, float]] | None = None
    if detector != "ffmpeg":
        try:
            raw = _detect_with_pyscenedetect(video, cfg)
        except Exception as exc:  # noqa: BLE001 - fall back, but say so
            log.warning("PySceneDetect failed on %s (%s); falling back to ffmpeg scene filter", video.name, exc)
            raw = None
    if raw is None:
        raw = _detect_with_ffmpeg(video, duration, cfg)
    if not raw:
        raw = [(0.0, duration)]
    return refine_scene_bounds(
        raw,
        duration,
        min_scene=float(cfg.get("media.min_scene_seconds", 0.8)),
        max_scene=float(cfg.get("media.max_scene_seconds", 12.0)),
        max_scenes=int(cfg.get("media.max_scenes_per_source", 40)),
    )


# --------------------------------------------------------------------------- #
# Frames + scenes.json
# --------------------------------------------------------------------------- #
def scene_content_hash(source_hash: str, start: float, end: float) -> str:
    return hash_text(f"{source_hash}|{start:.3f}|{end:.3f}")


def extract_scene_frames(scene: Scene, video: Path, paths: JobPaths, cfg: Config, *, force: bool = False) -> list[Path]:
    positions = list(cfg.get("media.frame_positions", [0.5, 0.25, 0.75]))
    width = int(cfg.get("media.frame_width", 768))
    quality = int(cfg.get("media.frame_quality", 3))
    out: list[Path] = []
    for pos in positions:
        label = f"{int(round(pos * 100)):02d}"
        dst = paths.frames_dir / f"scene_{scene.scene_id:03d}_{label}.jpg"
        if not dst.exists() or force:
            t = scene.start_time + (scene.end_time - scene.start_time) * float(pos)
            # Stay a few frames inside the scene so we never grab the next cut.
            t = min(max(scene.start_time + 0.05, t), max(scene.start_time, scene.end_time - 0.1))
            ff.extract_frame(video, t, dst, width=width, quality=quality)
        out.append(dst)
    return out


def photo_scenes(clip: SourceClip, photo_index: int, next_id: int, paths: JobPaths, cfg: Config, *, force: bool = False) -> list[Scene]:
    """Turn one normalized photo into framing scenes with a rendered frame each."""
    assert clip.normalized is not None
    photo = paths.work / clip.normalized
    width = int(clip.info.get("normalized_width") or clip.info.get("width") or 0)
    height = int(clip.info.get("normalized_height") or clip.info.get("height") or 0)
    if width <= 0 or height <= 0:
        raise PhotoError(f"{clip.file}: normalized photo has no dimensions recorded")
    frame_w = int(cfg.get("video.width", 1080))
    frame_h = int(cfg.get("video.height", 1920))
    hold = photo_hold_seconds(cfg)
    framings = plan_framings(
        width,
        height,
        frame_width=frame_w,
        frame_height=frame_h,
        index=photo_index,
        hold=hold,
        focus=detail_focus(photo),
        count=photo_framing_count(cfg),
        push_zoom=float(cfg.get("timeline.photo_push_zoom", 0.10)),
        detail_zoom=float(cfg.get("timeline.photo_detail_zoom", 1.45)),
    )
    out: list[Scene] = []
    for offset, motion in enumerate(framings):
        scene = Scene(
            scene_id=next_id + offset,
            source_file=clip.file,
            normalized_file=clip.normalized,
            start_time=0.0,
            end_time=hold,
            duration=hold,
            content_hash=hash_text(f"{clip.content_hash}|photo|{motion['framing']}|{hold:.3f}"),
            kind=PHOTO_KIND,
            motion=motion,
        )
        dst = paths.frames_dir / f"scene_{scene.scene_id:03d}_50.jpg"
        if not dst.exists() or force:
            render_frame(
                photo,
                dst,
                motion=motion,
                frame_width=frame_w,
                frame_height=frame_h,
                width=int(cfg.get("media.frame_width", 768)),
                quality=int(cfg.get("media.frame_quality", 3)),
            )
        scene.frames = [paths.rel(dst)]
        out.append(scene)
    log.info("%s: photo -> %d framing(s): %s", clip.file, len(out), ", ".join(str(s.framing) for s in out))
    return out


def build_scenes(clips: list[SourceClip], paths: JobPaths, cfg: Config, *, force: bool = False) -> dict[str, Any]:
    """Detect scenes for every usable clip (and framings for every photo), grab frames, write scenes.json."""
    scenes: list[Scene] = []
    next_id = 1
    photo_index = 0
    for clip in clips:
        if not clip.usable or not clip.normalized:
            continue
        if clip.is_photo:
            batch = photo_scenes(clip, photo_index, next_id, paths, cfg, force=force)
            scenes.extend(batch)
            next_id += len(batch)
            photo_index += 1
            continue
        video = paths.work / clip.normalized
        duration = float(clip.normalized_duration or 0.0)
        bounds = detect_scenes(video, duration, cfg)
        log.info("%s: %d scene(s)", clip.file, len(bounds))
        for start, end in bounds:
            scene = Scene(
                scene_id=next_id,
                source_file=clip.file,
                normalized_file=clip.normalized,
                start_time=start,
                end_time=end,
                duration=round(end - start, 3),
                content_hash=scene_content_hash(clip.content_hash, start, end),
            )
            frames = extract_scene_frames(scene, video, paths, cfg, force=force)
            scene.frames = [paths.rel(f) for f in frames]
            scenes.append(scene)
            next_id += 1
    doc = {
        "version": 1,
        "job": paths.name,
        "sources": [c.to_dict() for c in clips],
        "scenes": [s.to_dict() for s in scenes],
    }
    validate(doc, "scenes")
    write_json(paths.scenes_json, doc)
    return doc


def scenes_from_doc(doc: dict[str, Any]) -> list[Scene]:
    return [Scene(**{k: v for k, v in item.items() if k in Scene.__dataclass_fields__}) for item in doc["scenes"]]
