"""Job state file for resume-after-crash (Phase 15).

Each stage is recorded with a timestamp and optional info once it completes.
Re-running a job skips completed stages unless ``force`` is requested or a
later stage's inputs are invalidated with ``reset_from``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from autoeditor.logging_utils import get_logger

log = get_logger(__name__)

STAGES: tuple[str, ...] = (
    "discovered",
    "normalized",
    "analyzed",
    "scripted",
    "voiced",
    "captioned",
    "timeline_ready",
    "rendered",
    "qc_passed",
    "complete",
)

T = TypeVar("T")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StageError(RuntimeError):
    def __init__(self, stage: str, cause: BaseException) -> None:
        self.stage = stage
        self.cause = cause
        super().__init__(f"stage '{stage}' failed: {cause}")


class JobState:
    def __init__(self, path: Path, job: str, mode: str) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "job": job,
            "mode": mode,
            "created_at": _now(),
            "updated_at": _now(),
            "current_stage": None,
            "status": "running",
            "stages": {},
            "warnings": [],
            "errors": [],
        }

    # ------------------------------------------------------------------ #
    @classmethod
    def load_or_create(cls, path: Path, job: str, mode: str) -> JobState:
        state = cls(path, job, mode)
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("job") == job:
                    state.data.update(loaded)
                    state.data["status"] = "running"
                    log.info("Resuming job %s (completed stages: %s)", job, ", ".join(state.completed_stages()) or "none")
            except json.JSONDecodeError:
                log.warning("job_state.json is corrupt; starting fresh state for %s", job)
        return state

    def save(self) -> None:
        self.data["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------ #
    def is_done(self, stage: str) -> bool:
        return stage in self.data["stages"]

    def completed_stages(self) -> list[str]:
        return [s for s in STAGES if s in self.data["stages"]]

    def info(self, stage: str) -> dict[str, Any]:
        return dict(self.data["stages"].get(stage, {}).get("info", {}))

    def mark(self, stage: str, **info: Any) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage}")
        self.data["stages"][stage] = {"completed_at": _now(), "info": info}
        self.data["current_stage"] = stage
        self.save()

    def reset_from(self, stage: str) -> None:
        """Invalidate ``stage`` and everything after it."""
        idx = STAGES.index(stage)
        for later in STAGES[idx:]:
            self.data["stages"].pop(later, None)
        self.save()

    def add_warning(self, message: str) -> None:
        self.data["warnings"].append({"at": _now(), "message": message})
        self.save()

    def add_error(self, stage: str, message: str) -> None:
        self.data["errors"].append({"at": _now(), "stage": stage, "message": message})
        self.data["status"] = "failed"
        self.save()

    def finish(self, status: str = "complete") -> None:
        self.data["status"] = status
        self.save()

    # ------------------------------------------------------------------ #
    def run_stage(
        self,
        stage: str,
        fn: Callable[[], T],
        *,
        force: bool = False,
        skip_if_done: Callable[[], T] | None = None,
    ) -> T:
        """Run ``fn`` unless ``stage`` already completed (then call ``skip_if_done``).

        Failures are recorded in the state file and re-raised as StageError.
        """
        if self.is_done(stage) and not force:
            log.info("[%s] already complete - skipping", stage)
            if skip_if_done is None:
                raise RuntimeError(f"stage {stage} is complete but no loader was provided")
            return skip_if_done()
        log.info("[%s] starting", stage)
        self.data["current_stage"] = stage
        self.save()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - recorded then re-raised
            self.add_error(stage, f"{type(exc).__name__}: {exc}")
            raise StageError(stage, exc) from exc
        self.mark(stage)
        log.info("[%s] done", stage)
        return result
