from __future__ import annotations

from pathlib import Path

from autoeditor.config import load_config
from autoeditor.footage_only.shot_plan import plan_manual_script, split_manual_script
from autoeditor.pipeline.job import JobPaths
from autoeditor.providers.mock import MockLLM


def _inventory() -> dict:
    scenes = [
        {"scene_id": 1, "visual_interest_score": 95, "quality_score": 90, "description": "front view", "kind": "image"},
        {"scene_id": 2, "visual_interest_score": 80, "quality_score": 88, "description": "interior", "kind": "image"},
        {"scene_id": 3, "visual_interest_score": 75, "quality_score": 86, "description": "rear view", "kind": "image"},
    ]
    return {
        "usable_scenes": scenes,
        "strongest_scenes": [scenes[0], scenes[1]],
        "total_usable_seconds": 60,
    }


def test_split_manual_script_preserves_supplied_sentences() -> None:
    text = "This is my exact opening. Keep these words exactly! And this is the ending?"
    assert split_manual_script(text) == [
        "This is my exact opening.",
        "Keep these words exactly!",
        "And this is the ending?",
    ]


def test_manual_script_mock_planning_never_rewrites_narration(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, mock=True)
    paths = JobPaths.for_job(cfg, "manual")
    paths.ensure()
    source = "My exact first sentence.\nMy second sentence stays unchanged."
    script = plan_manual_script(source, _inventory(), "Demo topic", MockLLM(), cfg, paths, opener_hint=1)
    assert [line["narration"] for line in script["lines"]] == [
        "My exact first sentence.",
        "My second sentence stays unchanged.",
    ]
    assert script["lines"][0]["scene_ids"][0] == 1
    assert paths.script_json.exists()
