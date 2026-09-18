"""Normal mode orchestrator: topic/facts -> script -> voice -> captions -> media -> Remotion -> QC.

Applies the same duration policy, originality registry, watermark, REVIEW.md
checklist and human approval gate as footage-only mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoeditor.cache import JsonCache
from autoeditor.config import Config
from autoeditor.footage_only.shot_plan import estimate_seconds
from autoeditor.logging_utils import configure_logging, get_logger
from autoeditor.media.ffprobe import audio_duration
from autoeditor.pipeline import render as render_mod
from autoeditor.pipeline.audio_assets import select_music, select_sfx
from autoeditor.pipeline.branding import resolve_watermark
from autoeditor.pipeline.captions import build_captions
from autoeditor.pipeline.credits import SourceCredit, write_credits
from autoeditor.pipeline.duration import expansion_target, narration_shortfall, policy_from_config
from autoeditor.pipeline.job import JobPaths, read_json, sanitize_job_name, write_json
from autoeditor.pipeline.media_fetch import load_media, resolve_media
from autoeditor.pipeline.metadata import build_metadata
from autoeditor.pipeline.normal_timeline import build_normal_timeline
from autoeditor.pipeline.originality import OriginalityRegistry, OriginalityReport, script_fingerprint, variation_profile
from autoeditor.pipeline.qc import finalize_output, move_to_review, run_qc
from autoeditor.pipeline.review import NeedsReviewError, ReviewInfo, ai_disclosure, approval_status, write_review
from autoeditor.pipeline.script_writer import expand_script, read_facts, write_script
from autoeditor.pipeline.state import JobState, StageError
from autoeditor.pipeline.upload import upload_allowed, upload_to_youtube
from autoeditor.pipeline.voice import build_voice, load_voice_timing
from autoeditor.providers.base import TTSProvider
from autoeditor.providers.factory import build_providers
from autoeditor.providers.mock import MockTTS

log = get_logger(__name__)


@dataclass
class NormalOptions:
    topic: str
    job: str | None = None
    facts_file: Path | None = None
    skip_voice: bool = False
    skip_render: bool = False
    dry_run: bool = False
    upload: bool = False


def run_normal(cfg: Config, opts: NormalOptions) -> str:
    name = sanitize_job_name(opts.job or opts.topic[:40])
    paths = JobPaths.for_job(cfg, name)
    if opts.dry_run:
        log.info("DRY RUN normal mode: job=%s topic=%r facts=%s", name, opts.topic, opts.facts_file)
        log.info("  would write %s and %s", paths.work, paths.output)
        return "dry_run"
    paths.ensure()
    configure_logging(log_file=paths.log_file)
    state = JobState.load_or_create(paths.state_json, name, "normal")
    try:
        return _run_stages(name, cfg, opts, paths, state)
    except NeedsReviewError as exc:
        return _stop_for_review(name, opts, paths, state, str(exc))
    except StageError as exc:
        if isinstance(exc.cause, NeedsReviewError):
            return _stop_for_review(name, opts, paths, state, str(exc.cause))
        raise


def _stop_for_review(name: str, opts: NormalOptions, paths: JobPaths, state: JobState, reason: str) -> str:
    log.warning("Job %s stopped for human review: %s", name, reason)
    state.add_warning(reason)
    write_review(ReviewInfo(job=name, title=opts.topic, reason=reason), paths)
    state.finish("needs_review")
    return "needs_review"


def _run_stages(name: str, cfg: Config, opts: NormalOptions, paths: JobPaths, state: JobState) -> str:  # noqa: C901 - orchestration
    providers = build_providers(cfg, need_vision=False, need_tts=not opts.skip_voice, need_transcriber=not opts.skip_voice)
    facts = read_facts(opts.facts_file.read_text(encoding="utf-8")) if opts.facts_file and opts.facts_file.exists() else []
    llm_cache = JsonCache(paths.cache / "llm")
    policy = policy_from_config(cfg)
    raw_registry = Path(str(cfg.get("originality.registry_path", "cache/originality_registry.json")))
    registry = (
        OriginalityRegistry(raw_registry if raw_registry.is_absolute() else cfg.root / raw_registry) if bool(cfg.get("originality.enabled", True)) else None
    )
    tts: TTSProvider = providers.tts
    if opts.skip_voice:
        tts = MockTTS(words_per_second=policy.words_per_second)

    state.run_stage("discovered", lambda: None, skip_if_done=lambda: None)

    def make_script(attempt: int) -> dict[str, Any]:
        variation = variation_profile(f"{name}|{opts.topic}", attempt=attempt)
        script = write_script(opts.topic, facts, providers.llm, cfg, paths, cache=llm_cache, variation=variation)
        while (
            narration_shortfall(estimate_seconds(script["lines"], policy.words_per_second) + policy.outro_seconds, policy) > 0
            and int(script.get("expansions", 0)) < policy.max_expansions
        ):
            est = estimate_seconds(script["lines"], policy.words_per_second)
            longer = expand_script(script, facts, providers.llm, cfg, paths, current_seconds=est, add_seconds=expansion_target(est, policy), cache=llm_cache)
            if longer is None:
                break
            script = longer
        return script

    def scripted() -> dict[str, Any]:
        script = make_script(0)
        report = _check(registry, name, script, cfg)
        if report.verdict == "reject":
            script = make_script(1)
            report = _check(registry, name, script, cfg)
            if report.verdict == "reject":
                raise NeedsReviewError("near-duplicate of an earlier video: " + "; ".join(report.notes))
        write_json(paths.work / "originality.json", report.to_dict())
        return script

    script = state.run_stage("scripted", scripted, skip_if_done=lambda: read_json(paths.script_json))
    originality = read_json(paths.work / "originality.json") if (paths.work / "originality.json").exists() else None

    timings = state.run_stage("voiced", lambda: build_voice(script, paths, tts, cfg), skip_if_done=lambda: load_voice_timing(paths))
    if opts.skip_voice and not state.is_done("captioned"):
        state.add_warning("--skip-voice: placeholder tone track used")
    voice_duration = audio_duration(paths.voice_audio)
    while narration_shortfall(voice_duration + policy.outro_seconds, policy) > 0 and int(script.get("expansions", 0)) < policy.max_expansions:
        longer = expand_script(
            script, facts, providers.llm, cfg, paths, current_seconds=voice_duration, add_seconds=expansion_target(voice_duration, policy), cache=llm_cache
        )
        if longer is None:
            break
        script = longer
        timings = build_voice(script, paths, tts, cfg)
        voice_duration = audio_duration(paths.voice_audio)
        state.mark("scripted", expansions=script.get("expansions", 0))
        state.mark("voiced")
        state.reset_from("captioned")
    if narration_shortfall(voice_duration + policy.outro_seconds, policy) > 0:
        raise NeedsReviewError(
            f"narration is {voice_duration:.1f}s but a Short must be at least {policy.min_final_seconds:.0f}s; give the topic more substance and re-run"
        )

    captions = state.run_stage(
        "captioned",
        lambda: build_captions(script, timings, paths, providers.transcriber, cfg, voice_duration=voice_duration),
        skip_if_done=lambda: read_json(paths.captions_json),
    )
    media = state.run_stage("normalized", lambda: resolve_media(script, paths, cfg), skip_if_done=lambda: load_media(paths))
    state.run_stage("analyzed", lambda: None, skip_if_done=lambda: None)

    def timeline_ready() -> dict[str, Any]:
        music, music_credit = select_music(paths, cfg)
        sfx, sfx_credits = select_sfx(paths, cfg, line_starts=[t.start for t in timings])
        watermark, wm_warning = resolve_watermark(paths, cfg)
        if wm_warning:
            state.add_warning(wm_warning)
        tl = build_normal_timeline(script, timings, media, paths, cfg, voice_duration=voice_duration, music=music, sfx=sfx, watermark=watermark)
        media_credits = [SourceCredit(**m.credit) for m in media]
        license_warnings = write_credits(paths, script["title"], media_credits + ([music_credit] if music_credit else []) + sfx_credits)
        disclosure = ai_disclosure(media_credits)
        first = next((paths.work / m.src for m in media if m.type == "image"), None)
        build_metadata(
            script, paths, providers.llm, cfg, thumbnail_source=first, extra={"ai_disclosure": disclosure, "originality": originality, "review_required": True}
        )
        if registry is not None:
            registry.record(name, script_fingerprint(script), [], title=script["title"], variation=script.get("variation"))
        write_review(_info(name, script, tl, policy, license_warnings, originality, disclosure, state, qc=None), paths)
        return tl

    timeline = state.run_stage("timeline_ready", timeline_ready, skip_if_done=lambda: read_json(paths.timeline_json))
    if opts.skip_render:
        state.finish("timeline_ready")
        return "skipped_render"

    def rendered() -> Path:
        render_mod.stage_assets(paths, timeline, cfg)
        props = render_mod.build_props(paths, timeline, captions, script, cfg)
        render_mod.render_video(paths, cfg, props)
        return finalize_output(paths, cfg)

    final = state.run_stage("rendered", rendered, skip_if_done=lambda: paths.final_mp4)

    def qc() -> dict[str, Any]:
        result = run_qc(paths, cfg, expected_duration=float(timeline["duration"]), captions=captions, narration_end=voice_duration)
        if not result["passed"]:
            move_to_review(paths)
            raise RuntimeError("QC failed")
        return result

    metadata = read_json(paths.metadata_json) if paths.metadata_json.exists() else {}
    license_warnings = (
        [ln[2:] for ln in paths.credits_txt.read_text(encoding="utf-8").splitlines() if ln.startswith("! ")] if paths.credits_txt.exists() else []
    )
    try:
        qc_result = state.run_stage("qc_passed", qc, skip_if_done=lambda: read_json(paths.qc_json))
    except StageError:
        write_review(_info(name, script, timeline, policy, license_warnings, originality, metadata.get("ai_disclosure"), state, qc=None), paths)
        state.finish("needs_review")
        return "needs_review"
    write_review(_info(name, script, timeline, policy, license_warnings, originality, metadata.get("ai_disclosure"), state, qc=qc_result), paths)

    approved, approval_note = approval_status(paths)
    allowed, why = upload_allowed(cfg, requested=opts.upload, qc_passed=True, approved=approved)
    if allowed:
        state.mark("complete", uploaded=upload_to_youtube(final, metadata), approval=approval_note)
        state.finish("complete")
        return "complete"
    log.info("Upload skipped: %s", why)
    state.mark("complete", uploaded=None, approved=approved)
    if approved:
        state.finish("complete")
        log.info("Normal mode complete (approved) -> %s", final)
        return "complete"
    state.finish("awaiting_review")
    log.info("Normal mode rendered -> %s ; awaiting human review (%s)", final, paths.output / "REVIEW.md")
    return "awaiting_review"


def _check(registry: OriginalityRegistry | None, name: str, script: dict[str, Any], cfg: Config) -> OriginalityReport:
    if registry is None:
        return OriginalityReport(verdict="ok", notes=["originality registry disabled"])
    return registry.check(
        name,
        script_fingerprint(script),
        [],
        warn_at=float(cfg.get("originality.warn_similarity", 0.5)),
        reject_at=float(cfg.get("originality.reject_similarity", 0.8)),
    )


def _info(name, script, timeline, policy, license_warnings, originality, disclosure, state, *, qc):  # type: ignore[no-untyped-def]
    return ReviewInfo(
        job=name,
        title=script["title"],
        duration_seconds=float(timeline["duration"]),
        min_duration_seconds=policy.min_final_seconds,
        facts_to_verify=list(script.get("facts_to_verify", [])),
        license_warnings=license_warnings,
        originality=originality,
        ai_disclosure=disclosure,
        qc=qc,
        variation=script.get("variation"),
        warnings=[w["message"] for w in state.data.get("warnings", [])],
    )
