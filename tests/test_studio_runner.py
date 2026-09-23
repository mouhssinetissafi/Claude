from __future__ import annotations

from pathlib import Path

from autoeditor.studio.project import create_project
from autoeditor.studio.runner import StudioJobManager


def test_build_command_stages_project_without_touching_original(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"source-bytes")
    project = create_project(tmp_path / "project", "Demo")
    project.import_media([source])
    project.update_choices({"topic": "A demo topic", "mock_mode": True})
    manager = StudioJobManager()
    command = manager._build_command(project, "demo", mock=True, dry_run=True, prepare_only=False)
    staged = project.engine_dir / "inbox" / "demo" / "source.jpg"
    assert staged.exists()
    assert staged.read_bytes() == b"source-bytes"
    assert source.read_bytes() == b"source-bytes"
    assert "--mock" in command
    assert "--dry-run" in command
    assert (project.engine_dir / "inbox" / "demo" / "topic.txt").read_text(encoding="utf-8").strip() == "A demo topic"


def test_engine_config_uses_project_local_work_dirs(tmp_path: Path) -> None:
    project = create_project(tmp_path / "project", "Demo")
    manager = StudioJobManager()
    cfg = manager._write_engine_config(project)
    text = cfg.read_text(encoding="utf-8")
    assert str(project.engine_dir / "work") in text
    assert str(project.output_dir) in text
