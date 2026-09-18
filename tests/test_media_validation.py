from pathlib import Path

from autoeditor.footage_only.discovery import discover_job
from autoeditor.footage_only.normalize import normalize_clips, validate_clip
from autoeditor.media.ffprobe import MediaInfo, probe
from tests.conftest import make_clip, requires_ffmpeg


def _info(**kw) -> MediaInfo:  # type: ignore[no-untyped-def]
    base = dict(
        filename="c.mp4", path="c.mp4", duration=5.0, width=1920, height=1080, fps=30.0, codec="h264", aspect_ratio=1.78, has_audio=True, has_video=True
    )
    base.update(kw)
    return MediaInfo(**base)


def test_validate_clip_thresholds(cfg) -> None:
    cfg.set("media.min_clip_seconds", 1.0)
    cfg.set("media.min_width", 480)
    cfg.set("media.min_height", 480)
    assert validate_clip(_info(), cfg) is None
    assert "short" in validate_clip(_info(duration=0.5), cfg)
    assert "resolution" in validate_clip(_info(width=320, height=240), cfg)
    assert "duration" in validate_clip(_info(duration=0.0), cfg)
    assert "frame rate" in validate_clip(_info(fps=0.0), cfg)


@requires_ffmpeg
def test_corrupt_and_short_clips_are_rejected_and_good_ones_normalized(cfg, tmp_path: Path) -> None:
    from autoeditor.pipeline.job import JobPaths

    job_dir = tmp_path / "inbox" / "mixed"
    job_dir.mkdir(parents=True)
    make_clip(job_dir / "good.mp4", seconds=3.0, size="320x240", scenes=2)
    make_clip(job_dir / "short.mp4", seconds=0.4, size="320x240", scenes=1)
    (job_dir / "corrupt.mp4").write_bytes(b"garbage" * 500)
    job = discover_job(job_dir, cfg)
    paths = JobPaths.for_job(cfg, job.name, inbox=job_dir)
    paths.ensure()
    clips = normalize_clips(job, paths, cfg)
    by_name = {c.file: c for c in clips}
    assert by_name["corrupt.mp4"].status == "rejected" and "ffprobe" in (by_name["corrupt.mp4"].reason or "")
    assert by_name["short.mp4"].status == "rejected" and "short" in (by_name["short.mp4"].reason or "")
    good = by_name["good.mp4"]
    assert good.usable
    norm = paths.work / good.normalized
    info = probe(norm)
    assert (info.width, info.height) == (int(cfg.get("video.width")), int(cfg.get("video.height")))
    assert not info.has_audio  # -an
    assert abs(info.fps - 30) < 0.01
    # Original untouched, re-run reuses normalized output (mtime unchanged)
    m1 = norm.stat().st_mtime_ns
    normalize_clips(job, paths, cfg)
    assert norm.stat().st_mtime_ns == m1
    assert (job_dir / "good.mp4").exists()
