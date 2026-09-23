from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoeditor.studio.project import PROJECT_FILE, create_project, open_project


def test_create_and_open_project(tmp_path: Path) -> None:
    root = tmp_path / "My First Short"
    project = create_project(root, "My First Short")
    assert (root / PROJECT_FILE).exists()
    assert project.project_id == "my_first_short"
    assert project.media_dir.is_dir()
    reopened = open_project(root)
    assert reopened.to_dict() == project.to_dict()


def test_import_media_keeps_original_and_deduplicates(tmp_path: Path) -> None:
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"fake-jpeg-for-project-test")
    root = tmp_path / "project"
    project = create_project(root, "Project")
    imported = project.import_media([source])
    assert len(imported) == 1
    copied = root / imported[0].relative_path
    assert copied.read_bytes() == source.read_bytes()
    assert source.read_bytes() == b"fake-jpeg-for-project-test"
    second = project.import_media([source])
    assert second[0].id == imported[0].id
    assert len(project.media) == 1


def test_import_same_name_different_content_is_collision_safe(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "clip.mp4").write_bytes(b"one")
    (b / "clip.mp4").write_bytes(b"two")
    project = create_project(tmp_path / "project", "P")
    imported = project.import_media([a / "clip.mp4", b / "clip.mp4"])
    assert len({m.file_name for m in imported}) == 2
    assert len(project.media) == 2


def test_manual_script_can_be_drafted_but_generation_requires_text(tmp_path: Path) -> None:
    project = create_project(tmp_path / "project", "P")
    project.update_choices({"script_mode": "manual", "script_text": ""})
    with pytest.raises(ValueError, match="manual script"):
        project._validate_choices(require_ready=True)


def test_project_json_contains_no_credentials(tmp_path: Path) -> None:
    project = create_project(tmp_path / "project", "P")
    raw = json.loads(project.project_file.read_text(encoding="utf-8"))
    text = json.dumps(raw).lower()
    assert "api_key" not in text
    assert "anthropic_api_key" not in text
    assert "elevenlabs_api_key" not in text


def test_import_logo_is_copied_into_project(tmp_path: Path) -> None:
    from PIL import Image

    logo = tmp_path / "brand.png"
    Image.new("RGBA", (64, 64), (255, 0, 0, 128)).save(logo)
    project = create_project(tmp_path / "project-logo", "Logo")
    rel = project.import_logo(logo)
    assert rel == "branding/logo.png"
    assert (project.root_path / rel).exists()
    reopened = open_project(project.root_path)
    assert reopened.logo_relative_path == rel


def test_import_voice_is_copied_and_switches_mode(tmp_path: Path) -> None:
    voice = tmp_path / "voice.wav"
    voice.write_bytes(b"fake-wave-for-project-copy-test")
    project = create_project(tmp_path / "project-voice", "Voice")
    rel = project.import_voice(voice)
    assert rel == "audio/voice.wav"
    assert project.choices.voice_mode == "imported"
    assert (project.root_path / rel).read_bytes() == voice.read_bytes()
