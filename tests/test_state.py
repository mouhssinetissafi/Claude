from pathlib import Path

import pytest

from autoeditor.pipeline.state import STAGES, JobState, StageError


def test_run_stage_skips_completed_and_records_errors(tmp_path: Path) -> None:
    path = tmp_path / "job_state.json"
    state = JobState.load_or_create(path, "j", "footage_only")
    calls: list[str] = []
    assert state.run_stage("discovered", lambda: calls.append("run") or 1, skip_if_done=lambda: 99) == 1
    assert state.is_done("discovered")
    # Second call must not re-run the work.
    assert state.run_stage("discovered", lambda: calls.append("again") or 1, skip_if_done=lambda: 99) == 99
    assert calls == ["run"]

    def boom() -> None:
        raise ValueError("bad input")

    with pytest.raises(StageError) as exc:
        state.run_stage("normalized", boom, skip_if_done=lambda: None)
    assert exc.value.stage == "normalized"
    assert state.data["status"] == "failed"
    assert state.data["errors"][0]["stage"] == "normalized"
    assert not state.is_done("normalized")


def test_state_persists_and_resumes(tmp_path: Path) -> None:
    path = tmp_path / "job_state.json"
    s1 = JobState.load_or_create(path, "j", "footage_only")
    s1.mark("discovered")
    s1.mark("normalized", clips=3)
    s2 = JobState.load_or_create(path, "j", "footage_only")
    assert s2.completed_stages() == ["discovered", "normalized"]
    assert s2.info("normalized") == {"clips": 3}
    s2.reset_from("normalized")
    assert s2.completed_stages() == ["discovered"]
    # Different job name never inherits state.
    s3 = JobState.load_or_create(path, "other", "footage_only")
    assert s3.completed_stages() == []


def test_corrupt_state_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "job_state.json"
    path.write_text("{oops", encoding="utf-8")
    state = JobState.load_or_create(path, "j", "normal")
    assert state.completed_stages() == []
    with pytest.raises(ValueError):
        state.mark("not_a_stage")
    assert STAGES[-1] == "complete"
