from autoeditor.media.ffmpeg import FFmpegError, Interval, parse_blackdetect, parse_loudnorm, parse_silencedetect, parse_volumedetect
from autoeditor.media.ffprobe import MediaInfo
from autoeditor.pipeline.qc import evaluate_measurements
from autoeditor.schemas import validate

BLACK = "[blackdetect @ 0x1] black_start:0 black_end:1.5 black_duration:1.5\n[blackdetect @ 0x1] black_start:10.2 black_end:10.4 black_duration:0.2\n"
SILENCE = (
    "[silencedetect @ 0x2] silence_start: 3.1\n[silencedetect @ 0x2] silence_end: 6.4 | silence_duration: 3.3\n[silencedetect @ 0x2] silence_start: 40.0\n"
)
VOLUME = "[Parsed_volumedetect_0 @ 0x3] mean_volume: -18.3 dB\n[Parsed_volumedetect_0 @ 0x3] max_volume: -1.2 dB\n"
LOUDNORM = 'noise\n{\n\t"input_i" : "-20.10",\n\t"input_tp" : "-3.00",\n\t"input_lra" : "6.20",\n\t"input_thresh" : "-30.50",\n\t"target_offset" : "0.40"\n}\n'


def test_parsers() -> None:
    black = parse_blackdetect(BLACK)
    assert [(b.start, b.end) for b in black] == [(0.0, 1.5), (10.2, 10.4)]
    sil = parse_silencedetect(SILENCE, total_duration=42.0)
    assert [(s.start, s.end) for s in sil] == [(3.1, 6.4), (40.0, 42.0)]
    assert parse_volumedetect(VOLUME) == {"mean_volume": -18.3, "max_volume": -1.2}
    ln = parse_loudnorm(LOUDNORM)
    assert ln["input_i"] == "-20.10" and ln["target_offset"] == "0.40"
    try:
        parse_loudnorm("no json")
        raise AssertionError("expected FFmpegError")
    except FFmpegError:
        pass


def _info(**kw) -> MediaInfo:  # type: ignore[no-untyped-def]
    base = dict(
        filename="final.mp4",
        path="/o/final.mp4",
        duration=42.0,
        width=1080,
        height=1920,
        fps=30.0,
        codec="h264",
        aspect_ratio=0.5625,
        has_audio=True,
        has_video=True,
        audio_codec="aac",
    )
    base.update(kw)
    return MediaInfo(**base)


def test_qc_pass(cfg) -> None:
    cfg.set("video.width", 1080)
    cfg.set("video.height", 1920)
    result = evaluate_measurements(
        info=_info(),
        expected_duration=42.0,
        black=[],
        silence=[Interval(41.0, 42.0)],
        volume={"max_volume": -1.2},
        captions_end=41.2,
        narration_end=41.4,
        cfg=cfg,
    )
    validate(result, "qc")
    assert result["passed"], result["checks"]
    assert all(c["passed"] for c in result["checks"].values())


def test_qc_fails_on_black_silence_peak_resolution_captions(cfg) -> None:
    cfg.set("video.width", 1080)
    cfg.set("video.height", 1920)
    result = evaluate_measurements(
        info=_info(width=720, height=1280),
        expected_duration=42.0,
        black=[Interval(0.0, 1.5)],
        silence=[Interval(3.1, 6.4)],
        volume={"max_volume": 0.3},
        captions_end=45.0,
        narration_end=41.4,
        cfg=cfg,
    )
    validate(result, "qc")
    assert not result["passed"]
    failed = {k for k, v in result["checks"].items() if not v["passed"]}
    assert failed == {"resolution", "no_long_black", "no_long_silence", "audio_peak_safe", "captions_within_narration"}


def test_qc_missing_output(cfg) -> None:
    result = evaluate_measurements(info=None, expected_duration=10, black=[], silence=[], volume={}, captions_end=0, narration_end=0, cfg=cfg)
    assert not result["passed"] and not result["checks"]["output_exists"]["passed"]


def test_qc_duration_out_of_range(cfg) -> None:
    cfg.set("video.width", 1080)
    cfg.set("video.height", 1920)
    result = evaluate_measurements(
        info=_info(duration=90.0), expected_duration=42.0, black=[], silence=[], volume={"max_volume": -3}, captions_end=1, narration_end=2, cfg=cfg
    )
    assert not result["checks"]["duration"]["passed"]
