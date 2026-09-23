"""Newline-delimited JSON-RPC service used by the desktop application.

Run directly for development::

    python -m autoeditor.studio.service

One JSON object is read per stdin line and one response is written per request.
Asynchronous job events are emitted on stdout as objects with an ``event`` key.
No network port is opened, which keeps the desktop boundary local by default.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

from autoeditor import __version__
from autoeditor.studio.project import create_project, open_project
from autoeditor.studio.runner import StudioJobManager

SERVICE_VERSION = 1


class StudioService:
    def __init__(self, output: TextIO | None = None) -> None:
        self.output = output or sys.stdout
        self._write_lock = threading.Lock()
        self.jobs = StudioJobManager(event_sink=self._emit)

    def dispatch(self, method: str, params: dict[str, Any] | None = None) -> Any:
        p = params or {}
        if method == "ping":
            return {"ok": True, "service_version": SERVICE_VERSION, "engine_version": __version__}
        if method == "credentials.status":
            import os

            return {
                "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "elevenlabs": bool(os.environ.get("ELEVENLABS_API_KEY")),
                "elevenlabs_voice": bool(os.environ.get("ELEVENLABS_VOICE_ID")),
            }
        if method == "project.create":
            project = create_project(Path(str(p["path"])), p.get("name"))
            return project.to_dict()
        if method == "project.open":
            return open_project(Path(str(p["path"]))).to_dict()
        if method == "project.update":
            project = open_project(Path(str(p["path"])))
            project.update_choices(dict(p.get("choices", {})))
            return project.to_dict()
        if method == "project.import_media":
            project = open_project(Path(str(p["path"])))
            items = project.import_media([Path(str(v)) for v in p.get("files", [])])
            return {"project": project.to_dict(), "imported": [vars(item) for item in items]}
        if method == "project.import_logo":
            project = open_project(Path(str(p["path"])))
            project.import_logo(Path(str(p["file"])))
            return project.to_dict()
        if method == "project.import_voice":
            project = open_project(Path(str(p["path"])))
            project.import_voice(Path(str(p["file"])))
            return project.to_dict()
        if method == "project.remove_media":
            project = open_project(Path(str(p["path"])))
            project.remove_media(str(p["media_id"]), delete_file=bool(p.get("delete_file", True)))
            return project.to_dict()
        if method == "job.start":
            return self.jobs.start(Path(str(p["path"])), mock=p.get("mock"), dry_run=bool(p.get("dry_run", False)), prepare_only=bool(p.get("prepare_only", False)))
        if method == "job.cancel":
            return self.jobs.cancel(str(p["job_id"]))
        if method == "job.status":
            return self.jobs.get(str(p["job_id"]))
        if method == "job.list":
            return self.jobs.list()
        if method == "project.preview":
            project = open_project(Path(str(p["path"])))
            engine_name = __import__("autoeditor.pipeline.job", fromlist=["sanitize_job_name"]).sanitize_job_name(project.project_id or project.name)
            work = project.engine_dir / "work" / engine_name
            props_path = work / "render_props.json"
            if not props_path.exists():
                raise FileNotFoundError("preview is not prepared yet; click Generate first")
            props = json.loads(props_path.read_text(encoding="utf-8"))
            return {"props": props, "asset_root": str(work), "props_path": str(props_path)}
        raise KeyError(f"unknown method: {method}")

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        try:
            method = request.get("method")
            if not isinstance(method, str) or not method:
                raise ValueError("request.method must be a non-empty string")
            params = request.get("params", {})
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise ValueError("request.params must be an object")
            result = self.dispatch(method, params)
            return {"id": request_id, "result": result}
        except Exception as exc:  # noqa: BLE001 - protocol boundary
            return {"id": request_id, "error": {"type": type(exc).__name__, "message": str(exc)}}

    def serve(self, input_stream: TextIO | None = None) -> None:
        source = input_stream or sys.stdin
        for raw in source:
            raw = raw.strip()
            if not raw:
                continue
            try:
                request = json.loads(raw)
                if not isinstance(request, dict):
                    raise ValueError("request must be a JSON object")
                response = self.handle(request)
            except Exception as exc:  # noqa: BLE001
                response = {"id": None, "error": {"type": type(exc).__name__, "message": str(exc)}}
            self._write(response)

    def _emit(self, event: dict[str, Any]) -> None:
        self._write(event)

    def _write(self, payload: dict[str, Any]) -> None:
        with self._write_lock:
            self.output.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.output.flush()


def main() -> int:
    StudioService().serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
