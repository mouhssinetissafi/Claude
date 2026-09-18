"""Orchestrates the footage-only pipeline for every job in the inbox (Phases 1-15).

Each stage is wrapped by JobState.run_stage so a crash can be resumed without
repeating expensive work. Expensive AI results are additionally content-cached.

Safeguards applied along the way:
* footage sufficiency and a minimum final duration (see pipeline/duration.py)
* per-job variation profile and a cross-job originality registry
* license and AI-disclosure tracking, a REVIEW.md checklist and an explicit
  human approval marker before any upload
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoeditor.cache import JsonCache
from autoeditor.config import Config
from autoeditor.footage_only.discovery import DiscoveredJob, discover_jobs
from autoeditor.footage_only.inventory import build_inventory, write_inventory
from autoeditor.footage_only.normalize import SourceClip, load_manifest, normalize_clips
from autoeditor.footage_only.photos import PHOTO_KIND, PhotoError, photo_budget_seconds, photo_framing_count, photo_hold_seconds, probe_photo, validate_photo
from autoeditor.footage_only.scenes import Scene, build_scenes, scenes_from_doc
from autoeditor.footage_only.shot_plan import estimate_seconds, expand_shot_plan, generate_shot_plan
from autoeditor.footage_only.timeline import build_timeline
from autoeditor.footage_only.vision import analyze_scenes
from autoeditor.logging_utils import configure_logging, get_logger
from autoeditor.media.ffprobe import ProbeError, audio_duration, probe
from autoeditor.pipeline import render as render_mod
from autoeditor.pipeline.audio_assets import select_music, select_sfx
from autoeditor.pipeline.branding import resolve_watermark
from autoeditor.pipeline.captions import build_captions
from autoeditor.pipeline.credits import SourceCredit, write_credits
from autoeditor.pipeline.duration import DurationPolicy, expansion_target, footage_sufficient, narration_shortfall, policy_from_config
from autoeditor.pipeline.job import JobPaths, read_json, write_json
from autoeditor.pipeline.metadata import build_metadata
from autoeditor.pipeline.originality import OriginalityRegistry, OriginalityReport, script_fingerprint, variation_profile
from autoeditor.pipeline.qc import finalize_output, move_to_review, run_qc
from autoeditor.pipeline.review import NeedsReviewError, ReviewInfo, ai_disclosure, approval_status, write_review
from autoeditor.pipeline.state import JobState, StageError
from autoeditor.pipeline.upload import upload_allowed, upload_to_youtube
from autoeditor.pipeline.voice import LineTiming, build_voice, load_voice_timing
from autoeditor.providers.base import TTSProvider
from autoeditor.providers.factory import build_providers
from autoeditor.providers.mock import MockTTS
from autoeditor.style.profile import apply_to_config, influence_lines, load_or_build_profile, prompt_block

log = get_logger(__name__)


@dataclass
class FootageOnlyOptions:
    inbox: Path
    job: str | None = None
    skip_vision: bool = False
    skip_voice: bool = False
    skip_render: bool = False
    force_reanalyze: bool = False
    dry_run: bool = False
    upload: bool = False


@dataclass
class JobResult:
    name: str
    status: str  # complete | awaiting_review | needs_review | skipped_render | dry_run | failed
    final: Path | None = None
    message: str = ""


# --------------------------------------------------------------------------- #
def run_footage_only(cfg: Config, opts: FootageOnlyOptions) -> list[JobResult]:
    jobs = discover_jobs(opts.inbox, cfg, only=opts.job)
    if not jobs:
        log.warning("No jobs with media found in %s", opts.inbox)
        return []
    results: list[JobResult] = []
    for job in jobs:
        try:
            results.append(run_job(job, cfg, opts))
        except StageError as exc:
            log.error("Job %s failed at stage '%s': %s", job.name, exc.stage, exc.cause)
            results.append(JobResult(name=job.name, status="failed", message=f"{exc.stage}: {exc.cause}"))
    return results


def dry_run_job(job: DiscoveredJob, cfg: Config, paths: JobPaths) -> JobResult:
    """Inspect media and report the plan without encoding or calling any API."""
    log.info("DRY RUN for job %s (no encoding, no API calls, no files written)", job.name)
    from autoeditor.footage_only.normalize import validate_clip

    policy = policy_from_config(cfg)
    usable = 0
    total = 0.0
    for clip in job.clips:
        if job.kind_of(clip) == PHOTO_KIND:
            try:
                pinfo = probe_photo(clip)
            except PhotoError as exc:
                log.info("  %s -> REJECT (%s)", clip.name, exc)
                continue
            preason = validate_photo(pinfo, cfg)
            log.info(
                "  %s -> %s [photo %dx%d %s; %d framing(s) x %.1fs hold]",
                clip.name,
                f"REJECT ({preason})" if preason else "ok",
                pinfo.width,
                pinfo.height,
                pinfo.format,
                photo_framing_count(cfg),
                photo_hold_seconds(cfg),
            )
            if preason is None:
                usable += 1
                total += photo_budget_seconds(cfg)
            continue
        try:
            info = probe(clip)
        except ProbeError as exc:
            log.info("  %s -> REJECT (%s)", clip.name, exc)
            continue
        reason = validate_clip(info, cfg)
        status = f"REJECT ({reason})" if reason else "ok"
        log.info(
            "  %s -> %s [%dx%d %.1ffps %s %.2fs audio=%s]", clip.name, status, info.width, info.height, info.fps, info.codec, info.duration, info.has_audio
        )
        if reason is None:
            usable += 1
            total += info.duration
    ok, why = footage_sufficient(total, policy)
    log.info(
        "  usable media: %d (%d video, %d photo), total screen-time budget: %.1fs, topic: %s",
        usable,
        len(job.videos),
        len(job.photos),
        total,
        job.topic or "<infer>",
    )
    log.info("  duration policy: %s", why)
    for c in job.credits:
        log.info("  license %s: %s", c.file, c.license)
    state = JobState.load_or_create(paths.state_json, job.name, "footage_only") if paths.state_json.exists() else None
    done = state.completed_stages() if state else []
    log.info("  stages already complete: %s", ", ".join(done) or "none")
    log.info("  would write: %s, %s, %s", paths.work, paths.output, paths.cache)
    return JobResult(
        name=job.name,
        status="dry_run",
        message=f"{usable} usable clip(s), {total:.1f}s footage; {'ok' if ok else 'INSUFFICIENT for ' + str(int(policy.min_final_seconds)) + 's'}",
    )


def registry_path(cfg: Config) -> Path:
    raw = Path(str(cfg.get("originality.registry_path", "cache/originality_registry.json")))
    return raw if raw.is_absolute() else cfg.root / raw


# --------------------------------------------------------------------------- #
def run_job(job: DiscoveredJob, cfg: Config, opts: FootageOnlyOptions) -> JobResult:
    paths = JobPaths.for_job(cfg, job.name, inbox=job.inbox_dir)
    if opts.dry_run:
        return dry_run_job(job, cfg, paths)
    paths.ensure()
    configure_logging(log_file=paths.log_file)
    log.info("=== Job %s ===", job.name)
    state = JobState.load_or_create(paths.state_json, job.name, "footage_only")
    if opts.force_reanalyze:
        log.info("--force-reanalyze: invalidating analysis and later stages")
        state.reset_from("analyzed")
        JsonCache(paths.cache / "vision").clear()
        JsonCache(paths.cache / "llm").clear()
    try:
        return _run_stages(job, cfg, opts, paths, state)
    except NeedsReviewError as exc:
        return _stop_for_review(job, paths, state, str(exc))
    except StageError as exc:
        if isinstance(exc.cause, NeedsReviewError):
            return _stop_for_review(job, paths, state, str(exc.cause))
        raise


def _stop_for_review(job: DiscoveredJob, paths: JobPaths, state: JobState, reason: str) -> JobResult:
    log.warning("Job %s stopped for human review: %s", job.name, reason)
    state.add_warning(reason)
    write_review(ReviewInfo(job=job.name, title=job.topic or job.name, reason=reason), paths)
    state.finish("needs_review")
    return JobResult(name=job.name, status="needs_review", message=reason)


def _run_stages(job: DiscoveredJob, cfg: Config, opts: FootageOnlyOptions, paths: JobPaths, state: JobState) -> JobResult:  # noqa: C901 - orchestration
    providers = build_providers(cfg, need_llm=True, need_vision=not opts.skip_vision, need_tts=not opts.skip_voice, need_transcriber=not opts.skip_voice)
    vision_cache = JsonCache(paths.cache / "vision")
    llm_cache = JsonCache(paths.cache / "llm")
    policy = policy_from_config(cfg)
    registry = OriginalityRegistry(registry_path(cfg)) if bool(cfg.get("originality.enabled", True)) else None
    credits: list[SourceCredit] = list(job.credits)
    tts: TTSProvider = providers.tts
    if opts.skip_voice:
        tts = MockTTS(words_per_second=policy.words_per_second)

    # House style: weighted evidence from high-performing references (or the editorial baseline).
    style = load_or_build_profile(cfg)
    style_changes = apply_to_config(style, cfg)
    for change in style_changes:
        log.info("house style: %s", change)
    house_style = prompt_block(style) or None
    style_notes = influence_lines(style)
    if style is not None:
        write_json(paths.work / "style_profile.json", {**style, "applied_config_changes": style_changes})
    log.info("House style: %s", style_notes[0] if style_notes else "disabled")

    # Phase 1 -------------------------------------------------------------
    def discovered() -> dict[str, Any]:
        doc = job.to_dict()
        write_json(paths.work / "discovery.json", doc)
        return doc

    state.run_stage("discovered", discovered, skip_if_done=lambda: read_json(paths.work / "discovery.json"))

    # Phase 2 -------------------------------------------------------------
    clips: list[SourceClip] = state.run_stage("normalized", lambda: normalize_clips(job, paths, cfg), skip_if_done=lambda: load_manifest(paths))
    if not any(c.usable for c in clips):
        raise StageError("normalized", RuntimeError("no usable clips or photos after validation"))

    # Phases 3-5 ------------------------------------------------------------
    def analyze() -> tuple[dict[str, Any], list[Scene], dict[str, Any]]:
        doc = build_scenes(clips, paths, cfg, force=opts.force_reanalyze)
        scenes = scenes_from_doc(doc)
        doc = analyze_scenes(doc, scenes, paths, providers.vision, cfg, cache=vision_cache, skip_vision=opts.skip_vision, force=opts.force_reanalyze)
        inventory = build_inventory(scenes, cfg)
        write_inventory(inventory, paths)
        if not inventory["usable_scenes"]:
            raise RuntimeError("every scene was rejected (quality/watermark/safety); nothing to edit")
        return doc, scenes, inventory

    def load_analysis() -> tuple[dict[str, Any], list[Scene], dict[str, Any]]:
        doc = read_json(paths.scenes_json)
        return doc, scenes_from_doc(doc), read_json(paths.inventory_json)

    _scenes_doc, scenes, inventory = state.run_stage("analyzed", analyze, skip_if_done=load_analysis)

    # Duration policy: enough footage for a real Short? ------------------------
    enough, why = footage_sufficient(float(inventory.get("total_usable_seconds", 0.0)), policy)
    if not enough:
        raise NeedsReviewError(why)
    log.info("Footage check: %s", why)

    # Variation + originality inputs ------------------------------------------
    footage_signature = sorted({c.content_hash for c in clips if c.usable and c.content_hash})
    strongest = [int(s["scene_id"]) for s in inventory.get("strongest_scenes", [])]

    def make_script(attempt: int) -> dict[str, Any]:
        variation = variation_profile(f"{job.name}|{job.topic or ''}|{','.join(footage_signature)}", attempt=attempt)
        opener_hint = strongest[(int(variation["opener_rotation"]) + attempt) % len(strongest)] if strongest else None
        script = generate_shot_plan(
            inventory, job.topic, providers.llm, cfg, paths, cache=llm_cache, variation=variation, opener_hint=opener_hint, house_style=house_style
        )
        # Estimate-based expansion before spending on TTS.
        while (
            narration_shortfall(estimate_seconds(script["lines"], policy.words_per_second) + policy.outro_seconds, policy) > 0
            and int(script.get("expansions", 0)) < policy.max_expansions
        ):
            est = estimate_seconds(script["lines"], policy.words_per_second)
            longer = expand_shot_plan(
                script,
                inventory,
                providers.llm,
                cfg,
                paths,
                current_seconds=est,
                add_seconds=expansion_target(est, policy),
                cache=llm_cache,
                house_style=house_style,
            )
            if longer is None:
                break
            script = longer
        return script

    # Phase 6 -------------------------------------------------------------
    def scripted() -> dict[str, Any]:
        script = make_script(0)
        report = _originality_check(registry, job.name, script, footage_signature, cfg)
        if report.verdict == "reject":
            log.warning("Script too similar to an earlier job (%s); regenerating with a different angle", "; ".join(report.notes))
            script = make_script(1)
            report = _originality_check(registry, job.name, script, footage_signature, cfg)
            if report.verdict == "reject":
                raise NeedsReviewError("near-duplicate of an earlier video: " + "; ".join(report.notes))
        write_json(paths.work / "originality.json", report.to_dict())
        return script

    script = state.run_stage("scripted", scripted, skip_if_done=lambda: read_json(paths.script_json))
    originality = read_json(paths.work / "originality.json") if (paths.work / "originality.json").exists() else None

    # Phase 7 + measured duration -------------------------------------------------
    timings: list[LineTiming] = state.run_stage("voiced", lambda: build_voice(script, paths, tts, cfg), skip_if_done=lambda: load_voice_timing(paths))
    if opts.skip_voice and not state.is_done("captioned"):
        state.add_warning("--skip-voice: placeholder tone track used instead of narration")
    voice_duration = audio_duration(paths.voice_audio)
    while narration_shortfall(voice_duration + policy.outro_seconds, policy) > 0 and int(script.get("expansions", 0)) < policy.max_expansions:
        log.info("Narration is %.1fs; minimum is %.0fs - asking the writer for more useful context", voice_duration, policy.min_final_seconds)
        longer = expand_shot_plan(
            script,
            inventory,
            providers.llm,
            cfg,
            paths,
            current_seconds=voice_duration,
            add_seconds=expansion_target(voice_duration, policy),
            cache=llm_cache,
            house_style=house_style,
        )
        if longer is None:
            break
        script = longer
        timings = build_voice(script, paths, tts, cfg)  # unchanged lines are served from the per-line cache
        voice_duration = audio_duration(paths.voice_audio)
        state.mark("scripted", expansions=script.get("expansions", 0))
        state.mark("voiced")
        state.reset_from("captioned")
    shortfall = narration_shortfall(voice_duration + policy.outro_seconds, policy)
    if shortfall > 0:
        raise NeedsReviewError(
            f"narration is {voice_duration:.1f}s but a Short must be at least {policy.min_final_seconds:.0f}s; "
            f"the writer could not add enough useful context after {script.get('expansions', 0)} expansion(s). "
            "Give the job a richer topic.txt or more footage, then re-run."
        )

    # Phase 8 -------------------------------------------------------------
    captions = state.run_stage(
        "captioned",
        lambda: build_captions(script, timings, paths, providers.transcriber, cfg, voice_duration=voice_duration),
        skip_if_done=lambda: read_json(paths.captions_json),
    )

    # Phase 9 + 12 + 13 ----------------------------------------------------------
    def timeline_ready() -> dict[str, Any]:
        music, music_credit = select_music(paths, cfg)
        sfx, sfx_credits = select_sfx(paths, cfg, line_starts=[t.start for t in timings])
        watermark, wm_warning = resolve_watermark(paths, cfg)
        if wm_warning:
            state.add_warning(wm_warning)
        tl = build_timeline(script, timings, scenes, inventory, paths, cfg, voice_duration=voice_duration, music=music, sfx=sfx, watermark=watermark)
        # Credit only footage that actually appears in the video.
        used_scene_ids = {seg.get("scene_id") for line in tl["lines"] for seg in line["segments"]}
        used_files = {s.source_file for s in scenes if s.scene_id in used_scene_ids}
        footage_credits = [c for c in credits if c.file in used_files]
        all_credits = footage_credits + ([music_credit] if music_credit else []) + sfx_credits
        license_warnings = write_credits(paths, script["title"], all_credits)
        disclosure = ai_disclosure(footage_credits)
        if disclosure["review_required"]:
            log.warning("AI disclosure review required: %s", ", ".join(disclosure["flagged_files"]))
        best = _best_frame(scenes, inventory, paths)
        build_metadata(
            script, paths, providers.llm, cfg, thumbnail_source=best, extra={"ai_disclosure": disclosure, "originality": originality, "review_required": True}
        )
        if registry is not None:
            registry.record(job.name, script_fingerprint(script), footage_signature, title=script["title"], variation=script.get("variation"))
        write_review(_review_info(job, script, tl, policy, license_warnings, originality, disclosure, state, qc=None, style=style_notes), paths)
        return tl

    timeline = state.run_stage("timeline_ready", timeline_ready, skip_if_done=lambda: read_json(paths.timeline_json))

    if opts.skip_render:
        log.info("--skip-render: stopping after timeline. Render later with the same command without --skip-render.")
        state.finish("timeline_ready")
        return JobResult(name=job.name, status="skipped_render", message=str(paths.timeline_json))

    # Phase 10 ------------------------------------------------------------
    def rendered() -> Path:
        render_mod.stage_assets(paths, timeline, cfg)
        props = render_mod.build_props(paths, timeline, captions, script, cfg)
        render_mod.render_video(paths, cfg, props)
        return finalize_output(paths, cfg)

    final = state.run_stage("rendered", rendered, skip_if_done=lambda: paths.final_mp4)

    # Phase 11 ------------------------------------------------------------
    def qc() -> dict[str, Any]:
        result = run_qc(paths, cfg, expected_duration=float(timeline["duration"]), captions=captions, narration_end=voice_duration)
        if not result["passed"]:
            move_to_review(paths)
            raise RuntimeError("QC failed: " + ", ".join(k for k, v in result["checks"].items() if not v["passed"]))
        return result

    metadata = read_json(paths.metadata_json) if paths.metadata_json.exists() else {}
    license_warnings = (
        [ln[2:] for ln in paths.credits_txt.read_text(encoding="utf-8").splitlines() if ln.startswith("! ")] if paths.credits_txt.exists() else []
    )
    try:
        qc_result = state.run_stage("qc_passed", qc, skip_if_done=lambda: read_json(paths.qc_json))
    except StageError as exc:
        write_review(
            _review_info(
                job,
                script,
                timeline,
                policy,
                license_warnings,
                originality,
                metadata.get("ai_disclosure"),
                state,
                qc=read_json(paths.qc_json) if paths.qc_json.exists() else None,
                style=style_notes,
            ),
            paths,
        )
        state.finish("needs_review")
        return JobResult(name=job.name, status="needs_review", final=final, message=str(exc.cause))
    write_review(
        _review_info(job, script, timeline, policy, license_warnings, originality, metadata.get("ai_disclosure"), state, qc=qc_result, style=style_notes), paths
    )

    # Human approval gate + upload (never automatic) ------------------------------
    approved, approval_note = approval_status(paths)
    allowed, why = upload_allowed(cfg, requested=opts.upload, qc_passed=True, approved=approved)
    if allowed:
        video_id = upload_to_youtube(final, metadata)
        state.mark("complete", uploaded=video_id, approval=approval_note)
        state.finish("complete")
        log.info("Job %s uploaded as %s", job.name, video_id)
        return JobResult(name=job.name, status="complete", final=final, message=f"uploaded {video_id}")
    log.info("Upload skipped: %s", why)
    state.mark("complete", uploaded=None, approved=approved)
    if approved:
        state.finish("complete")
        log.info("Job %s complete (approved) -> %s", job.name, final)
        return JobResult(name=job.name, status="complete", final=final, message=approval_note)
    state.finish("awaiting_review")
    log.info("Job %s rendered -> %s ; awaiting human review (%s)", job.name, final, paths.output / "REVIEW.md")
    return JobResult(name=job.name, status="awaiting_review", final=final, message=f"review {paths.output / 'REVIEW.md'}")


# --------------------------------------------------------------------------- #
def _originality_check(registry: OriginalityRegistry | None, job: str, script: dict[str, Any], footage: list[str], cfg: Config) -> OriginalityReport:
    if registry is None:
        return OriginalityReport(verdict="ok", notes=["originality registry disabled"])
    report = registry.check(
        job,
        script_fingerprint(script),
        footage,
        warn_at=float(cfg.get("originality.warn_similarity", 0.5)),
        reject_at=float(cfg.get("originality.reject_similarity", 0.8)),
    )
    for note in report.notes:
        log.info("originality: %s", note)
    return report


def _review_info(
    job: DiscoveredJob,
    script: dict[str, Any],
    timeline: dict[str, Any],
    policy: DurationPolicy,
    license_warnings: list[str],
    originality: dict[str, Any] | None,
    disclosure: dict[str, Any] | None,
    state: JobState,
    *,
    qc: dict[str, Any] | None,
    style: list[str] | None = None,
) -> ReviewInfo:
    return ReviewInfo(
        job=job.name,
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
        style_influences=list(style or []),
    )


def _best_frame(scenes: list[Scene], inventory: dict[str, Any], paths: JobPaths) -> Path | None:
    strongest = inventory.get("strongest_scenes") or []
    by_id = {s.scene_id: s for s in scenes}
    for entry in strongest:
        scene = by_id.get(int(entry["scene_id"]))
        if scene and scene.frames:
            candidate = paths.work / scene.frames[0]
            if candidate.exists():
                return candidate
    return None
