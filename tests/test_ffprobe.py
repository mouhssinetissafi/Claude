from pathlib import Path

import pytest

from autoeditor.media.ffprobe import ProbeError, parse_ffprobe_json, probe
from tests.conftest import make_clip, requires_ffmpeg

SAMPLE = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
            "r_frame_rate": "30000/1001",
            "duration": "12.512000",
        },
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
    ],
    "format": {"duration": "12.520000", "size": "123456", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
}


def test_parse_basic_fields() -> None:
    info = parse_ffprobe_json(SAMPLE, Path("/x/clip.mp4"))
    assert info.filename == "clip.mp4"
    assert info.width == 1920 and info.height == 1080
    assert info.duration == 12.512
    assert round(info.fps, 2) == 29.97
    assert info.codec == "h264"
    assert info.has_audio and info.audio_codec == "aac"
    assert info.aspect_ratio == round(1920 / 1080, 4)
    assert info.size_bytes == 123456


def test_rotation_swaps_dimensions() -> None:
    payload = {"streams": [dict(SAMPLE["streams"][0], tags={"rotate": "90"})], "format": {}}
    info = parse_ffprobe_json(payload, "phone.mp4")
    assert (info.width, info.height) == (1080, 1920)
    assert info.rotation == 90
    assert not info.has_audio


def test_missing_video_stream_is_error() -> None:
    with pytest.raises(ProbeError):
        parse_ffprobe_json({"streams": [SAMPLE["streams"][1]], "format": {}}, "audio.m4a")


def test_duration_fallback_to_format() -> None:
    stream = dict(SAMPLE["streams"][0])
    stream.pop("duration")
    info = parse_ffprobe_json({"streams": [stream], "format": {"duration": "3.5"}}, "a.mp4")
    assert info.duration == 3.5


def test_unreadable_duration_warns() -> None:
    stream = dict(SAMPLE["streams"][0], duration="N/A")
    info = parse_ffprobe_json({"streams": [stream], "format": {"duration": "N/A"}}, "a.mp4")
    assert info.duration == 0.0
    assert any("duration" in w for w in info.warnings)


@requires_ffmpeg
def test_probe_real_file(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "c.mp4", seconds=2.0, size="320x240")
    info = probe(clip)
    assert (info.width, info.height) == (320, 240)
    assert 1.8 <= info.duration <= 2.2
    assert info.has_audio


@requires_ffmpeg
def test_probe_corrupt_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00\x01\x02 this is not a video" * 100)
    with pytest.raises(ProbeError):
        probe(bad)
    with pytest.raises(ProbeError):
        probe(tmp_path / "missing.mp4")


def test_ffprobe_binary_can_be_overridden_by_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from autoeditor.media import ffprobe as module

    fake = tmp_path / "ffprobe.exe"
    fake.write_bytes(b"stub")
    monkeypatch.setenv("AUTOEDITOR_FFPROBE", str(fake))
    assert module.ffprobe_available()
    assert module._ffprobe_binary() == str(fake)
