from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from autoeditor.studio import runner as runner_mod
from autoeditor.studio.project import create_project
from autoeditor.studio.runner import PREPARE_STAGES, StudioJob, StudioJobManager, child_environment
from tests.conftest import make_photo, requires_ffmpeg

LONG_SCRIPT = "\n".join(
    [
        "This SUV looks calm from far away, and that calm is carefully designed.",
        "Up close, every surface has a job, and nothing is there by accident.",
        "The wheels are large and sharp, and they fill the arches without looking heavy.",
        "The headlights draw one clean line across the front of the car.",
        "The hood is long and quiet, with a single crease that catches the light.",
        "The badge sits low and small, so the shape does the talking.",
        "Inside, the seats look soft and simple, with stitching that follows every curve.",
        "The dashboard stays clean, with fewer buttons than you would expect.",
        "Even the door panels match the seats, so the cabin reads as one piece.",
        "From the side, the roof line runs straight and calm to the tail lights.",
        "The tail lights echo the front, which ties the whole design together.",
        "Nothing on this car shouts for attention, and that is the point.",
        "It is built to feel expensive without ever needing to say so.",
        "Look again at the first shot, and it reads differently now.",
    ]
)


def _wait(manager: StudioJobManager, job_id: str, seconds: float = 180) -> dict[str, Any]:
    deadline = time.time() + seconds
    while time.time() < deadline:
        job = manager.get(job_id)
        if job["status"] not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(0.2)
    raise AssertionError(f"job still {manager.get(job_id)['status']} after {seconds}s")


@pytest.fixture
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the cross-job originality registry out of the repository during these runs."""
    original = StudioJobManager._write_engine_config

    def patched(self: StudioJobManager, project):  # type: ignore[no-untyped-def]
        path = original(self, project)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"originality:\n  registry_path: '{(tmp_path / 'registry.json').as_posix()}'\n")
        return path

    monkeypatch.setattr(StudioJobManager, "_write_engine_config", patched)


def _photo_project(tmp_path: Path, script: str):  # type: ignore[no-untyped-def]
    photos = [make_photo(tmp_path / "src" / f"photo_{i:02d}.jpg", size=(640, 480), detail_at=((i % 3 + 1) / 4, (i // 3 + 1) / 4)) for i in range(9)]
    project = create_project(tmp_path / "My Short", "My Short")
    project.import_media(photos)
    project.update_choices({"script_mode": "manual", "script_text": script, "mock_mode": True})
    return project


def test_child_environment_resets_pyinstaller_only_when_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in child_environment()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert child_environment()["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_stage_lines_drive_step_and_label() -> None:
    manager = StudioJobManager()
    job = StudioJob(id="j", project_root=".", engine_job_name="x", status="running", prepare_only=True, stage_total=len(PREPARE_STAGES))
    assert manager._track_line(job, "12:00:00 INFO    autoeditor.pipeline.state: [discovered] starting")
    assert (job.stage_index, job.stage_label) == (1, "Reading your media")
    assert manager._track_line(job, "12:00:01 INFO    autoeditor.pipeline.state: [analyzed] starting")
    assert (job.stage_index, job.stage_label) == (3, "Analyzing scenes")
    assert manager._track_line(job, "12:00:02 INFO    autoeditor.pipeline.state: [analyzed] done")
    assert (job.stage, job.stage_index, job.stage_label) == ("scripted", 4, "Planning the script and shots")
    assert not manager._track_line(job, "12:00:02 INFO    autoeditor.pipeline.state: [scripted] starting")  # already there
    assert not manager._track_line(job, "some other log line")
    manager._track_line(job, "[timeline_ready] done")
    assert (job.stage_index, job.stage_label) == (7, "Finishing up")


@requires_ffmpeg
def test_prepare_job_with_photos_and_manual_script_reports_stages(tmp_path: Path, isolated_registry: None) -> None:
    project = _photo_project(tmp_path, LONG_SCRIPT)
    events: list[dict[str, Any]] = []
    manager = StudioJobManager(event_sink=events.append)
    job = _wait(manager, manager.start(project.root_path, mock=True, prepare_only=True)["id"])
    assert job["status"] == "prepared", job["message"]
    labels = [e["params"]["job"]["stage_label"] for e in events if e["event"] == "job.status" and e["params"]["job"]["status"] == "running"]
    steps = {e["params"]["job"]["stage_index"] for e in events if e["event"] == "job.status"}
    assert "Starting the engine" in labels and "Analyzing scenes" in labels and "Building the timeline" in labels
    assert steps >= set(range(1, len(PREPARE_STAGES) + 1))


@requires_ffmpeg
def test_engine_review_reason_reaches_the_job_message(tmp_path: Path, isolated_registry: None) -> None:
    project = _photo_project(tmp_path, "Too short to be a Short.")
    manager = StudioJobManager()
    job = _wait(manager, manager.start(project.root_path, mock=True, prepare_only=True)["id"])
    assert job["status"] == "needs_review"
    assert "minimum final duration" in job["message"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group check; Windows uses taskkill /T")
def test_cancel_stops_the_whole_process_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "a.jpg"
    make_photo(source, size=(640, 480))
    project = create_project(tmp_path / "p", "P")
    project.import_media([source])
    project.update_choices({"mock_mode": True})
    spawn = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print('GRANDCHILD', p.pid, flush=True)\n"
        "time.sleep(120)\n"
    )
    monkeypatch.setattr(StudioJobManager, "_build_command", lambda self, *a, **k: [sys.executable, "-c", spawn])
    lines: list[str] = []
    manager = StudioJobManager(event_sink=lambda e: lines.append(e["params"]["line"]) if e["event"] == "job.log" else None)
    job_id = manager.start(project.root_path, mock=True, prepare_only=True)["id"]
    deadline = time.time() + 30
    while not any(line.startswith("GRANDCHILD") for line in lines) and time.time() < deadline:
        time.sleep(0.1)
    grandchild = int(next(line for line in lines if line.startswith("GRANDCHILD")).split()[1])
    child = manager.get(job_id)["pid"]
    cancelled = manager.cancel(job_id)
    assert cancelled["status"] == "cancelled"
    assert _wait(manager, job_id, 30)["status"] == "cancelled"  # never overwritten as "failed"

    def gone(pid: int) -> bool:
        status = Path(f"/proc/{pid}/status")
        return not status.exists() or "State:\tZ" in status.read_text()

    deadline = time.time() + 10
    while not (gone(child) and gone(grandchild)) and time.time() < deadline:
        time.sleep(0.1)
    assert gone(child) and gone(grandchild)
    assert runner_mod.kill_process_tree  # used by cancel


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
