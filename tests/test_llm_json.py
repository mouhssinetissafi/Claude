import pytest

from autoeditor.providers.anthropic_provider import simplify_schema_for_output
from autoeditor.schemas import SCENE_ANALYSIS_SCHEMA, LLMJsonError, extract_json_object


def test_plain_json() -> None:
    assert extract_json_object('{"a": 1, "b": [1, 2]}') == {"a": 1, "b": [1, 2]}


def test_fenced_json_with_prose() -> None:
    text = 'Sure! Here is the plan:\n```json\n{"title": "x", "lines": []}\n```\nLet me know.'
    assert extract_json_object(text) == {"title": "x", "lines": []}


def test_json_with_surrounding_prose_and_nested_braces() -> None:
    text = 'Result: {"a": {"b": {"c": "}"}}, "s": "brace } in string"} trailing'
    assert extract_json_object(text) == {"a": {"b": {"c": "}"}}, "s": "brace } in string"}


def test_invalid_json_raises() -> None:
    with pytest.raises(LLMJsonError):
        extract_json_object("no json here")
    with pytest.raises(LLMJsonError):
        extract_json_object("[1, 2, 3]")  # arrays are not objects
    with pytest.raises(LLMJsonError):
        extract_json_object("")


def test_simplify_schema_strips_unsupported_keywords() -> None:
    out = simplify_schema_for_output(SCENE_ANALYSIS_SCHEMA)
    assert "$schema" not in out
    assert "minimum" not in out["properties"]["quality_score"]
    assert out["properties"]["quality_score"]["type"] == "number"
    assert out["additionalProperties"] is False
    # Original untouched
    assert "minimum" in SCENE_ANALYSIS_SCHEMA["properties"]["quality_score"]
