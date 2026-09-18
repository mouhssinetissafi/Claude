from pathlib import Path

from autoeditor.logging_utils import redact
from autoeditor.pipeline.credits import LICENSE_UNKNOWN, SourceCredit, find_license, render_credits


def test_find_license_sidecar_variants(tmp_path: Path) -> None:
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    assert find_license(clip).license == LICENSE_UNKNOWN
    (tmp_path / "a.license.json").write_text('{"license": "CC BY 4.0", "author": "Ann", "url": "https://x"}', encoding="utf-8")
    c = find_license(clip)
    assert c.license == "CC BY 4.0" and c.author == "Ann"
    (tmp_path / "a.license.json").unlink()
    (tmp_path / "credits.json").write_text('{"default": {"license": "Pexels License", "source": "Pexels"}}', encoding="utf-8")
    assert find_license(clip).license == "Pexels License"
    (tmp_path / "credits.json").write_text("{bad json", encoding="utf-8")
    assert find_license(clip).license == LICENSE_UNKNOWN


def test_render_credits_warns_on_unknown() -> None:
    text, warnings = render_credits(
        "job",
        "Title",
        [
            SourceCredit(file="a.mp4"),
            SourceCredit(file="b.mp4", license="CC0", source="Pixabay"),
            SourceCredit(file="bed.mp3", license="Royalty free", kind="music"),
        ],
    )
    assert "LICENSE_UNKNOWN" in text
    assert len(warnings) == 1 and "a.mp4" in warnings[0]
    assert "MUSIC" in text and "FOOTAGE" in text


def test_redact_masks_keys(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-SECRETSECRETSECRET")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "abcdef1234567890abcdef")
    text = "key sk-ant-api03-SECRETSECRETSECRET and xi-api-key: abcdef1234567890abcdef done"
    out = redact(text)
    assert "SECRETSECRET" not in out
    assert "abcdef1234567890abcdef" not in out
    assert "redacted" in out
