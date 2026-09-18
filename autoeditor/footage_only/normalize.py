"""Phase 2: media validation and normalization.

Each clip is probed, validated against the configured thresholds and, when
usable, normalized into ``work/<job>/normalized/`` as 1080x1920 30fps H.264
yuv420p with scale+crop (never stretched) and audio removed. Photos are
normalized with Pillow instead: EXIF orientation applied, metadata stripped,
bounded size, saved as JPEG. Normalization is skipped when an up-to-date
output already exists (content hash sidecar).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from autoeditor.cache import hash_file
from autoeditor.config import Config
from autoeditor.footage_only.discovery import DiscoveredJob
from autoeditor.footage_only.photos import PHOTO_KIND, VIDEO_KIND, PhotoError, normalize_photo, probe_photo, validate_photo
from autoeditor.logging_utils import get_logger
from autoeditor.media import ffmpeg as ff
from autoeditor.media.ffprobe import MediaInfo, ProbeError, probe
from autoeditor.pipeline.job import JobPaths, read_json, write_json

log = get_logger(__name__)


@dataclass
class SourceClip:
    file: str
    path: str
    content_hash: str
    status: str  # ok | rejected
    reason: str | None = None
    normalized: str | None = None  # relative to work dir
    normalized_duration: float | None = None  # videos only; photos have a hold budget instead
    info: dict[str, Any] = field(default_factory=dict)
    kind: str = VIDEO_KIND  # video | image

    @property
    def usable(self) -> bool:
        return self.status == "ok" and self.normalized is not None

    @property
    def is_photo(self) -> bool:
        return self.kind == PHOTO_KIND

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_clip(info: MediaInfo, cfg: Config) -> str | None:
    """Return a rejection reason or None when the clip passes validation."""
    min_seconds = float(cfg.get("media.min_clip_seconds", 1.0))
    min_w = int(cfg.get("media.min_width", 480))
    min_h = int(cfg.get("media.min_height", 480))
    if not info.has_video:
        return "no video stream"
    if info.duration <= 0:
        return "duration unreadable (corrupt container?)"
    if info.duration < min_seconds:
        return f"too short ({info.duration:.2f}s < {min_seconds}s)"
    short_side = min(info.width, info.height)
    if short_side < min(min_w, min_h) or max(info.width, info.height) < max(min_w, min_h):
        return f"resolution too low ({info.width}x{info.height})"
    if info.fps <= 0:
        return "frame rate unreadable"
    return None


def _normalize_photo(clip: Path, index: int, content_hash: str, paths: JobPaths, cfg: Config, *, force: bool) -> SourceClip:
    record = SourceClip(file=clip.name, path=str(clip), content_hash=content_hash, status="ok", kind=PHOTO_KIND)
    try:
        info = probe_photo(clip)
    except PhotoError as exc:
        record.status, record.reason = "rejected", f"photo unreadable: {exc}"
        log.warning("%s: rejected - %s", clip.name, record.reason)
        return record
    record.info = info.to_dict()
    reason = validate_photo(info, cfg)
    if reason is not None:
        record.status, record.reason = "rejected", reason
        log.warning("%s: rejected - %s", clip.name, reason)
        return record
    target = paths.normalized_dir / f"{index:02d}_{clip.stem}.jpg"
    sidecar = target.with_name(target.name + ".meta.json")
    up_to_date = not force and target.exists() and sidecar.exists() and read_json(sidecar).get("content_hash") == content_hash
    if up_to_date:
        log.info("%s: normalized photo up to date", clip.name)
    else:
        log.info(
            "%s: normalizing photo -> %s (%dx%d %s%s)",
            clip.name,
            target.name,
            info.width,
            info.height,
            info.format,
            ", EXIF rotation applied" if info.orientation_applied else "",
        )
        try:
            out = normalize_photo(clip, target, max_edge=int(cfg.get("media.photo_max_edge", 3840)), quality=int(cfg.get("media.photo_quality", 92)))
        except PhotoError as exc:
            record.status, record.reason = "rejected", f"normalization failed: {exc}"
            log.error("%s: %s", clip.name, record.reason)
            return record
        record.info.update({"normalized_width": out.width, "normalized_height": out.height})
        write_json(sidecar, {"content_hash": content_hash, "source": str(clip), "width": out.width, "height": out.height})
    meta = read_json(sidecar)
    record.info.setdefault("normalized_width", int(meta.get("width", info.width)))
    record.info.setdefault("normalized_height", int(meta.get("height", info.height)))
    record.normalized = paths.rel(target)
    return record


def normalize_clips(job: DiscoveredJob, paths: JobPaths, cfg: Config, *, force: bool = False) -> list[SourceClip]:
    """Probe, validate and normalize every clip and photo; write media_manifest.json."""
    results: list[SourceClip] = []
    video = cfg.section("video")
    for index, clip in enumerate(job.clips, start=1):
        try:
            content_hash = hash_file(clip)
        except OSError as exc:
            results.append(SourceClip(file=clip.name, path=str(clip), content_hash="", status="rejected", reason=f"unreadable: {exc}", kind=job.kind_of(clip)))
            log.warning("%s: unreadable (%s)", clip.name, exc)
            continue
        if job.kind_of(clip) == PHOTO_KIND:
            results.append(_normalize_photo(clip, index, content_hash, paths, cfg, force=force))
            continue
        try:
            info = probe(clip)
        except ProbeError as exc:
            results.append(SourceClip(file=clip.name, path=str(clip), content_hash=content_hash, status="rejected", reason=f"ffprobe failed: {exc}"))
            log.warning("%s: rejected - %s", clip.name, exc)
            continue
        reason = validate_clip(info, cfg)
        record = SourceClip(
            file=clip.name, path=str(clip), content_hash=content_hash, status="ok" if reason is None else "rejected", reason=reason, info=info.to_dict()
        )
        if reason is not None:
            log.warning("%s: rejected - %s", clip.name, reason)
            results.append(record)
            continue
        target = paths.normalized_dir / f"{index:02d}_{clip.stem}.mp4"
        sidecar = target.with_suffix(".mp4.meta.json")
        up_to_date = not force and target.exists() and sidecar.exists() and read_json(sidecar).get("content_hash") == content_hash
        if up_to_date:
            log.info("%s: normalized output up to date", clip.name)
        else:
            log.info("%s: normalizing -> %s (%dx%d %.1ffps %s)", clip.name, target.name, info.width, info.height, info.fps, info.codec)
            try:
                ff.normalize_video(
                    clip,
                    target,
                    width=int(video["width"]),
                    height=int(video["height"]),
                    fps=int(video["fps"]),
                    crf=int(video["crf"]),
                    preset=str(video.get("preset", "medium")),
                    pix_fmt=str(video["pix_fmt"]),
                    codec=str(video["codec"]),
                )
            except ff.FFmpegError as exc:
                record.status = "rejected"
                record.reason = f"normalization failed: {exc}"
                log.error("%s: %s", clip.name, record.reason)
                results.append(record)
                continue
            write_json(sidecar, {"content_hash": content_hash, "source": str(clip)})
        try:
            norm_info = probe(target)
        except ProbeError as exc:
            record.status = "rejected"
            record.reason = f"normalized file unreadable: {exc}"
            results.append(record)
            continue
        record.normalized = paths.rel(target)
        record.normalized_duration = norm_info.duration
        results.append(record)
    usable = [r for r in results if r.usable]
    log.info(
        "Normalization: %d usable (%d video, %d photo), %d rejected",
        len(usable),
        sum(1 for r in usable if not r.is_photo),
        sum(1 for r in usable if r.is_photo),
        len(results) - len(usable),
    )
    write_json(paths.media_manifest_json, {"version": 1, "job": paths.name, "clips": [r.to_dict() for r in results]})
    return results


def load_manifest(paths: JobPaths) -> list[SourceClip]:
    data = read_json(paths.media_manifest_json)
    return [SourceClip(**item) for item in data["clips"]]
