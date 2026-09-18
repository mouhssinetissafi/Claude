"""Originality safeguards, AI-disclosure flag and the human approval gate."""

from __future__ import annotations

import json
from pathlib import Path

from autoeditor.pipeline.credits import SourceCredit, find_license
from autoeditor.pipeline.job import JobPaths
from autoeditor.pipeline.originality import HOOK_STYLES, OriginalityRegistry, jaccard, script_fingerprint, shingles, variation_profile
from autoeditor.pipeline.review import APPROVAL_FILE, ReviewInfo, ai_disclosure, approval_status, render_review, write_review
from autoeditor.pipeline.upload import upload_allowed
from autoeditor.providers.mock import MockLLM
from autoeditor.schemas import LLM_SHOT_PLAN_SCHEMA, validate
from tests.conftest import requires_ffmpeg
from tests.test_shot_plan import make_inventory


def _script(lines: list[str], title: str = "t") -> dict:
    return {"title": title, "description": "", "lines": [{"id": i, "narration": t} for i, t in enumerate(lines, start=1)]}


def test_variation_profile_is_deterministic_and_varies() -> None:
    a = variation_profile("job-a|topic")
    assert a == variation_profile("job-a|topic")
    assert variation_profile("job-a|topic", attempt=1) != a
    hooks = {variation_profile(f"job-{i}|topic")["hook_style"] for i in range(40)}
    assert len(hooks) >= min(4, len(HOOK_STYLES))


def test_fingerprint_similarity() -> None:
    s1 = _script(["Apple made thinness the whole point of this phone.", "The edges catch the light and the weight disappears."])
    s2 = _script(["Apple made thinness the whole point of this phone.", "The edges catch the light and the weight disappears."])
    s3 = _script(["A completely different opening about a kitchen.", "Nothing here overlaps with the phone script at all."])
    f1, f2, f3 = script_fingerprint(s1), script_fingerprint(s2), script_fingerprint(s3)
    assert jaccard(f1["shingles"], f2["shingles"]) == 1.0
    assert jaccard(f1["shingles"], f3["shingles"]) < 0.1
    assert f1["hook"] == f2["hook"] and f1["hook"] != f3["hook"]
    assert shingles(["a", "b"]) == {"a b"} and shingles([]) == set()


def test_registry_rejects_near_duplicates_and_ignores_self(tmp_path: Path) -> None:
    reg = OriginalityRegistry(tmp_path / "reg.json")
    base = _script(
        [
            "Start with the shape. Every line is doing a job.",
            "Look at how the light moves across it. Nothing here is accidental.",
            "Put it all together and the pattern is clear.",
        ]
    )
    reg.record("job_a", script_fingerprint(base), ["hash1", "hash2"], title="A", variation=None)
    assert (tmp_path / "reg.json").exists()

    dup = reg.check("job_b", script_fingerprint(base), ["hash9"])
    assert dup.verdict == "reject" and dup.most_similar_job == "job_a" and dup.max_similarity == 1.0

    same_job = reg.check("job_a", script_fingerprint(base), ["hash1", "hash2"])
    assert same_job.verdict == "ok"  # re-running a job never collides with itself

    fresh = _script(
        [
            "A calm street at dawn, and a door that stays closed.",
            "Then the light changes and the street fills up.",
            "By noon the door is open and nobody looks twice.",
        ]
    )
    ok = reg.check("job_c", script_fingerprint(fresh), ["hash1", "hash2"])
    assert ok.verdict == "ok" and any("same footage" in n for n in ok.notes)

    hooked = _script(
        [
            "Start with the shape. Every line is doing a job.",
            "Everything after this line is new and different from before.",
            "And a closing line that shares nothing either.",
        ]
    )
    warn = reg.check("job_d", script_fingerprint(hooked), [])
    assert warn.verdict == "warn" and warn.same_hook_jobs == ["job_a"]

    # Same footage plus a half-similar script is a near-duplicate video.
    half = _script(
        [
            "Start with the shape. Every line is doing a job.",
            "Look at how the light moves across it. Nothing here is accidental.",
            "A brand new closing thought about the weather.",
        ]
    )
    near = reg.check("job_e", script_fingerprint(half), ["hash1", "hash2"], warn_at=0.3, reject_at=0.9)
    assert near.verdict == "reject" and any("near-duplicate" in n for n in near.notes)

    reloaded = OriginalityRegistry(tmp_path / "reg.json")
    assert "job_a" in reloaded.entries


def test_mock_llm_varies_with_profile(cfg) -> None:
    from autoeditor.footage_only.shot_plan import build_user_prompt

    inv = make_inventory()
    llm = MockLLM()
    plans = []
    for job in ("job_one", "job_two", "job_three"):
        user = build_user_prompt(inv, "why the iPhone Air is thin", cfg, variation=variation_profile(f"{job}|topic"), opener_hint=None)
        data = llm.complete_json(system="", user=user, output_schema=LLM_SHOT_PLAN_SCHEMA, max_tokens=10).data
        validate(data, "llm_shot_plan")
        plans.append(data)
    openers = {p["lines"][0]["narration"] for p in plans}
    assert len(openers) >= 2  # hooks differ between jobs
    sims = [
        jaccard(script_fingerprint(a)["shingles"], script_fingerprint(b)["shingles"])
        for a, b in [(plans[0], plans[1]), (plans[1], plans[2]), (plans[0], plans[2])]
    ]
    assert max(sims) < 0.8


def test_ai_disclosure_flag_only_for_footage() -> None:
    plain = [SourceCredit(file="a.mp4", license="CC0"), SourceCredit(file="bed.mp3", kind="music", ai_generated=True)]
    d = ai_disclosure(plain)
    assert d["review_required"] is False and d["flagged_files"] == []
    flagged = ai_disclosure([SourceCredit(file="gen.mp4", ai_generated=True), SourceCredit(file="edit.mp4", altered=True), SourceCredit(file="real.mp4")])
    assert flagged["review_required"] is True and flagged["flagged_files"] == ["gen.mp4", "edit.mp4"]


def test_license_sidecar_ai_flags(tmp_path: Path) -> None:
    clip = tmp_path / "gen.mp4"
    clip.write_bytes(b"x")
    (tmp_path / "gen.license.json").write_text('{"license": "CC0", "ai_generated": "yes"}', encoding="utf-8")
    credit = find_license(clip)
    assert credit.ai_generated and not credit.altered and "ai_generated" not in credit.extra
    (tmp_path / "gen.license.json").write_text('{"license": "CC0", "altered": true}', encoding="utf-8")
    assert find_license(clip).altered


def test_upload_requires_human_approval(cfg, monkeypatch) -> None:
    cfg.set("upload.enabled", True)
    for var in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
        monkeypatch.setenv(var, "x")
    assert upload_allowed(cfg, requested=True, qc_passed=True, approved=False)[0] is False
    assert "approval" in upload_allowed(cfg, requested=True, qc_passed=True, approved=False)[1]
    assert upload_allowed(cfg, requested=True, qc_passed=True, approved=True)[0] is True
    assert upload_allowed(cfg, requested=True, qc_passed=False, approved=True)[0] is False
    assert upload_allowed(cfg, requested=False, qc_passed=True, approved=True)[0] is False


def test_approval_marker_and_review_file(cfg) -> None:
    paths = JobPaths.for_job(cfg, "rev")
    paths.ensure()
    approved, note = approval_status(paths)
    assert not approved and "REVIEW.md" in note
    info = ReviewInfo(
        job="rev",
        title="Title",
        duration_seconds=52.0,
        min_duration_seconds=45,
        facts_to_verify=["Verify the release year"],
        license_warnings=["clip02.mov: license unknown"],
        originality={"verdict": "warn", "max_similarity": 0.55, "notes": ["script is 55% similar to job 'x'"]},
        ai_disclosure={"review_required": True, "flagged_files": ["gen.mp4"], "note": "flagged"},
        qc={"passed": True, "checks": {}},
    )
    text = render_review(info, paths)
    for needle in ("Verify the release year", "license unknown", "55%", "REVIEW REQUIRED", "gen.mp4", APPROVAL_FILE, "52.0s"):
        assert needle in text
    write_review(info, paths)
    (paths.output / APPROVAL_FILE).write_text("approved by tester", encoding="utf-8")
    assert approval_status(paths) == (True, "approved by tester")


@requires_ffmpeg
def test_end_to_end_records_registry_and_waits_for_review(inbox: Path, cfg) -> None:
    from autoeditor.footage_only.runner import FootageOnlyOptions, registry_path, run_footage_only

    results = run_footage_only(cfg, FootageOnlyOptions(inbox=inbox, job="iphone_air", skip_render=True, skip_vision=True, skip_voice=True))
    assert results[0].status == "skipped_render", results[0].message
    paths = JobPaths.for_job(cfg, "iphone_air")
    assert (paths.work / "originality.json").exists()
    assert (paths.output / "REVIEW.md").exists()
    metadata = json.loads(paths.metadata_json.read_text(encoding="utf-8"))
    validate(metadata, "metadata")
    assert metadata["review_required"] is True and metadata["ai_disclosure"]["review_required"] is False
    reg = json.loads(registry_path(cfg).read_text(encoding="utf-8"))
    assert "iphone_air" in reg["jobs"]
    script = json.loads(paths.script_json.read_text(encoding="utf-8"))
    assert script["variation"]["hook_style"]
