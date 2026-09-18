from pathlib import Path

import pytest

from autoeditor.footage_only.discovery import discover_job, discover_jobs, read_topic
from autoeditor.pipeline.credits import LICENSE_UNKNOWN


def test_discovers_jobs_with_and_without_topic(inbox: Path, cfg) -> None:
    jobs = discover_jobs(inbox, cfg)
    names = [j.name for j in jobs]
    assert names == ["iphone_air", "no_topic"]  # empty_job has no media and is skipped
    a = jobs[0]
    assert a.topic == "why the iPhone Air is so thin"
    assert [c.name for c in a.clips] == ["clip01.mp4", "clip02.mov"]
    assert any("notes.docx" in i for i in a.ignored)
    b = jobs[1]
    assert b.topic is None and b.topic_file is None


def test_missing_topic_file(tmp_path: Path) -> None:
    assert read_topic(tmp_path) == (None, None)
    (tmp_path / "topic.txt").write_text("   \n", encoding="utf-8")
    topic, path = read_topic(tmp_path)
    assert topic is None and path is not None


def test_topic_whitespace_is_collapsed(tmp_path: Path) -> None:
    (tmp_path / "topic.txt").write_text("  why   the\n iPhone Air\tfailed ", encoding="utf-8")
    assert read_topic(tmp_path)[0] == "why the iPhone Air failed"


def test_job_filter_and_missing_job(inbox: Path, cfg) -> None:
    only = discover_jobs(inbox, cfg, only="no_topic")
    assert [j.name for j in only] == ["no_topic"]
    with pytest.raises(FileNotFoundError):
        discover_jobs(inbox, cfg, only="does_not_exist")


def test_missing_inbox(tmp_path: Path, cfg) -> None:
    with pytest.raises(FileNotFoundError):
        discover_jobs(tmp_path / "nope", cfg)


def test_license_sidecars_are_carried(inbox: Path, cfg) -> None:
    job = discover_job(inbox / "iphone_air", cfg)
    by_file = {c.file: c for c in job.credits}
    assert by_file["clip01.mp4"].license == "Pexels License"
    assert by_file["clip01.mp4"].author == "Test Author"
    assert by_file["clip02.mov"].license == LICENSE_UNKNOWN


def test_originals_are_untouched(inbox: Path, cfg) -> None:
    before = {p.name: p.stat().st_mtime_ns for p in (inbox / "iphone_air").iterdir()}
    discover_job(inbox / "iphone_air", cfg)
    after = {p.name: p.stat().st_mtime_ns for p in (inbox / "iphone_air").iterdir()}
    assert before == after
