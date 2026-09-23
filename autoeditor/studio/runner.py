"""Run the existing engine as an isolated child process for Studio.

Keeping generation in a child process gives the desktop application three useful
properties without changing the proven pipeline: logs can be streamed as progress,
a generation can be cancelled by terminating the child, and a pipeline crash cannot
bring down the UI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from autoeditor.config import REPO_ROOT
from autoeditor.pipeline.job import sanitize_job_name
from autoeditor.studio.project import StudioProject, open_project

EventSink = Callable[[dict[str, Any]], None]


@dataclass
class StudioJob:
    id: str
    project_root: str
    engine_job_name: str
    status: str = "queued"  # queued | running | cancelling | cancelled | prepared | complete | needs_review | failed
    started_at: float | None = None
    ended_at: float | None = None
    return_code: int | None = None
    message: str = ""
    command: list[str] = field(default_factory=list)
    final_path: str | None = None
    pid: int | None = None
    prepare_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StudioJobManager:
    def __init__(self, event_sink: EventSink | None = None) -> None:
        self._event_sink = event_sink or (lambda _event: None)
        self._jobs: dict[str, StudioJob] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.to_dict() for job in self._jobs.values()]

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"job not found: {job_id}")
            return self._jobs[job_id].to_dict()

    def start(self, project_root: Path, *, mock: bool | None = None, dry_run: bool = False, prepare_only: bool = False) -> dict[str, Any]:
        project = open_project(project_root)
        if not project.media:
            raise ValueError("project has no media")
        project._validate_choices(require_ready=True)
        job_id = uuid.uuid4().hex[:12]
        engine_name = sanitize_job_name(project.project_id or project.name)
        command = self._build_command(project, engine_name, mock=mock, dry_run=dry_run, prepare_only=prepare_only)
        job = StudioJob(id=job_id, project_root=str(project.root_path), engine_job_name=engine_name, command=command, prepare_only=prepare_only)
        with self._lock:
            self._jobs[job_id] = job
        project.last_job_id = job_id
        project.save()
        thread = threading.Thread(target=self._run, args=(job_id,), name=f"studio-job-{job_id}", daemon=True)
        thread.start()
        return job.to_dict()

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"job not found: {job_id}")
            proc = self._processes.get(job_id)
            if job.status not in {"queued", "running"}:
                return job.to_dict()
            job.status = "cancelling"
        self._emit("job.status", job)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
            except OSError:
                pass
        with self._lock:
            job.status = "cancelled"
            job.ended_at = time.time()
            job.message = "Cancelled by user"
        self._emit("job.status", job)
        return job.to_dict()

    def _build_command(self, project: StudioProject, engine_name: str, *, mock: bool | None, dry_run: bool, prepare_only: bool = False) -> list[str]:
        inbox = self._stage_project(project, engine_name)
        config_path = self._write_engine_config(project)
        use_mock = project.choices.mock_mode if mock is None else mock
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--engine-cli"]
        else:
            command = [sys.executable, str(REPO_ROOT / "run.py")]
        command += [
            "--config",
            str(config_path),
            "--footage-only",
            "--inbox",
            str(inbox),
            "--job",
            engine_name,
            "--theme",
            project.choices.theme,
        ]
        if use_mock:
            command.append("--mock")
        if project.choices.voice_mode == "placeholder":
            command.append("--skip-voice")
        if dry_run:
            command.append("--dry-run")
        if prepare_only:
            command.append("--skip-render")
        return command

    def _stage_project(self, project: StudioProject, engine_name: str) -> Path:
        inbox = project.engine_dir / "inbox"
        job_dir = inbox / engine_name
        if job_dir.exists():
            shutil.rmtree(job_dir)
        job_dir.mkdir(parents=True, exist_ok=True)
        for item in project.media:
            source = project.root_path / item.relative_path
            if not source.exists():
                raise FileNotFoundError(f"project media is missing: {source}")
            destination = job_dir / item.file_name
            try:
                os.link(source, destination)
            except OSError:
                shutil.copy2(source, destination)
        topic = project.choices.topic.strip()
        if topic:
            (job_dir / "topic.txt").write_text(topic + "\n", encoding="utf-8")
        # The proven footage-only engine does not consume manual scripts yet.
        # Persist it beside the staged job now so the upcoming manual-script bridge
        # can adopt it without changing the project format.
        if project.choices.script_mode == "manual":
            (job_dir / "script.txt").write_text(project.choices.script_text.strip() + "\n", encoding="utf-8")
        if project.choices.voice_mode == "imported" and project.choices.voice_file:
            voice_source = project.root_path / project.choices.voice_file
            if not voice_source.exists():
                raise FileNotFoundError(f"project narration is missing: {voice_source}")
            shutil.copy2(voice_source, job_dir / ("voice_import" + voice_source.suffix.lower()))
        return inbox

    def _write_engine_config(self, project: StudioProject) -> Path:
        engine = project.engine_dir
        engine.mkdir(parents=True, exist_ok=True)
        config_path = engine / "studio.yaml"
        data: dict[str, Any] = {
            "project": {
                "work_dir": str(engine / "work"),
                "output_dir": str(project.output_dir),
                "cache_dir": str(engine / "cache"),
                "review_dir": str(engine / "review"),
                "inbox_dir": str(engine / "inbox"),
            },
            "branding": {
                "watermark_enabled": bool(project.choices.watermark_enabled),
                "position": project.choices.watermark_position,
                "opacity": float(project.choices.watermark_opacity),
                "width_fraction": float(project.choices.watermark_width_fraction),
            },
        }
        if project.logo_relative_path:
            logo = project.root_path / project.logo_relative_path
            if logo.exists():
                data["branding"]["logo_path"] = str(logo)
        remotion_dir = os.environ.get("AUTOEDITOR_REMOTION_DIR", "").strip()
        if remotion_dir:
            data["render"] = {"remotion_dir": remotion_dir}
        config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return config_path

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.status == "cancelled":
                return
            job.status = "running"
            job.started_at = time.time()
            command = list(job.command)
        self._emit("job.status", job)
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            with self._lock:
                self._processes[job_id] = proc
                job.pid = proc.pid
            if proc.stdout is not None:
                for line in proc.stdout:
                    clean = line.rstrip("\r\n")
                    if clean:
                        self._event_sink({"event": "job.log", "params": {"job_id": job_id, "line": clean}})
            code = proc.wait()
            project = open_project(Path(job.project_root))
            final = project.output_dir / job.engine_job_name / "final.mp4"
            with self._lock:
                if job.status == "cancelling":
                    job.status = "cancelled"
                    job.message = "Cancelled by user"
                elif code == 0:
                    if job.prepare_only:
                        props = project.engine_dir / "work" / job.engine_job_name / "render_props.json"
                        job.status = "prepared" if props.exists() else "needs_review"
                        job.message = "Edit prepared for preview" if props.exists() else "Preparation finished but preview props are missing"
                    else:
                        job.status = "complete" if final.exists() else "needs_review"
                        job.message = "Generation finished" if final.exists() else "Generation finished; review may be required"
                else:
                    job.status = "failed"
                    job.message = f"Engine exited with code {code}"
                job.return_code = code
                job.ended_at = time.time()
                job.final_path = str(final) if final.exists() else None
        except Exception as exc:  # noqa: BLE001 - boundary turns crashes into job state
            with self._lock:
                job.status = "failed"
                job.ended_at = time.time()
                job.message = str(exc)
        finally:
            with self._lock:
                self._processes.pop(job_id, None)
            self._emit("job.status", job)

    def _emit(self, event_name: str, job: StudioJob) -> None:
        self._event_sink({"event": event_name, "params": {"job": job.to_dict()}})
