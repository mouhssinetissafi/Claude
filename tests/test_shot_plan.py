from pathlib import Path

import pytest

from autoeditor.footage_only.shot_plan import build_user_prompt, estimate_seconds, find_banned_phrases, generate_shot_plan, numeric_claims, repair_plan
from autoeditor.pipeline.job import JobPaths
from autoeditor.providers.base import LLMResult, ProviderError
from autoeditor.providers.mock import MockLLM
from autoeditor.schemas import LLM_SHOT_PLAN_SCHEMA, validate


def make_inventory(n: int = 6) -> dict:
    scenes = [
        {
            "scene_id": i,
            "source_file": f"clip{(i - 1) // 3 + 1}.mp4",
            "duration": 3.0,
            "description": f"scene {i}",
            "subjects": ["phone"],
            "objects": [],
            "environment": "studio",
            "shot_type": "close-up",
            "camera_motion": "static",
            "subject_motion": "",
            "mood": "",
            "text_content": "",
            "product_or_brand": [],
            "quality_score": 80,
            "visual_interest_score": 50 + i * 5,
            "score": 60 + i * 3,
            "similar_to": [],
            "duplicate_group": i,
        }
        for i in range(1, n + 1)
    ]
    return {
        "version": 1,
        "total_usable_seconds": 3.0 * n,
        "usable_scene_count": n,
        "usable_scenes": scenes,
        "strongest_scenes": [{"scene_id": n, "score": 99, "description": "best"}, {"scene_id": n - 1, "score": 90, "description": "second"}],
        "recurring_subjects": [{"subject": "phone", "count": n}],
        "products_or_brands": [],
        "possible_topics": [],
        "duplicate_groups": [],
        "rejected_scenes": [],
    }


def test_mock_llm_plan_matches_schema(cfg) -> None:
    llm = MockLLM()
    user = build_user_prompt(make_inventory(), "why the iPhone Air is thin", cfg)
    result = llm.complete_json(system="", user=user, output_schema=LLM_SHOT_PLAN_SCHEMA, max_tokens=100)
    validate(result.data, "llm_shot_plan")
    assert result.data["lines"][0]["scene_ids"]


def test_repair_enforces_rules(cfg) -> None:
    inv = make_inventory()
    plan = {
        "title": "t",
        "description": "d",
        "facts_to_verify": [],
        "lines": [
            {"id": 7, "narration": "Opens on a weak scene.", "scene_ids": [1, 1, 99], "overlay_text": "  ", "emphasis_words": ["missing", "weak"]},
            {"id": 8, "narration": "   ", "scene_ids": [2], "overlay_text": None, "emphasis_words": []},
            {"id": 9, "narration": "It sold 5 million units in 2024 for $999.", "scene_ids": [3], "overlay_text": "SOLD", "emphasis_words": []},
        ],
    }
    repaired, notes = repair_plan(plan, inv, cfg)
    lines = repaired["lines"]
    assert [ln["id"] for ln in lines] == [1, 2]  # empty line dropped, ids renumbered
    assert lines[0]["scene_ids"][0] == 6  # strongest scene prepended
    assert 99 not in lines[0]["scene_ids"]
    assert lines[0]["scene_ids"].count(1) == 1  # consecutive duplicate removed
    assert lines[0]["emphasis_words"] == ["weak"]  # emphasis must appear in narration
    assert lines[0]["overlay_text"] is None
    assert any("numeric" in f.lower() or "price" in f.lower() for f in repaired["facts_to_verify"])
    assert any("unknown scene" in n for n in notes)


def test_banned_phrases_and_numeric_claims() -> None:
    lines = [
        {"id": 1, "narration": "Did you know this is insane?"},
        {"id": 2, "narration": "It weighs 165 grams."},
        {"id": 3, "narration": "The edges catch the light."},
    ]
    hits = find_banned_phrases(lines, ["did you know", "insane", "i tested"])
    assert len(hits) == 2
    claims = numeric_claims(lines)
    assert len(claims) == 1 and "Line 2" in claims[0]
    assert estimate_seconds(lines, 2.6) > 0


class BannedThenCleanLLM:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, *, system, user, output_schema, max_tokens):  # type: ignore[no-untyped-def]
        self.calls += 1
        narration = "Did you know this is insane?" if self.calls == 1 else "The edges catch the light."
        return LLMResult(
            data={
                "title": "t",
                "description": "d",
                "facts_to_verify": [],
                "lines": [{"id": 1, "narration": narration, "scene_ids": [6], "overlay_text": None, "emphasis_words": []}],
            },
            model="fake",
        )


def test_generate_plan_retries_on_banned_phrase(cfg, tmp_path: Path) -> None:
    paths = JobPaths.for_job(cfg, "job")
    paths.ensure()
    llm = BannedThenCleanLLM()
    script = generate_shot_plan(make_inventory(), None, llm, cfg, paths)
    assert llm.calls == 2
    assert script["lines"][0]["narration"] == "The edges catch the light."
    assert script["mode"] == "footage_only" and script["topic"] is None
    assert paths.script_json.exists()


def test_generate_plan_fails_without_scenes(cfg, tmp_path: Path) -> None:
    paths = JobPaths.for_job(cfg, "job")
    with pytest.raises(ProviderError):
        generate_shot_plan({"usable_scenes": []}, "x", MockLLM(), cfg, paths)
