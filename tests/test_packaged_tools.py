from pathlib import Path

import pytest


def test_ffmpeg_binary_can_be_overridden_by_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from autoeditor.media import ffmpeg as module

    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"stub")
    monkeypatch.setenv("AUTOEDITOR_FFMPEG", str(fake))
    assert module.ffmpeg_available()
    assert module._ffmpeg_binary() == str(fake)
