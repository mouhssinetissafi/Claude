"""Orchestrates the footage-only pipeline for every job in the inbox (Phases 1-15).

Each stage is wrapped by JobState.run_stage so a crash can be resumed without
repeating expensive work. Expensive AI results are additionally content-cached.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoeditor.cache import JsonCache
from autoeditor.config import Config
from autoeditor.footage_only.discovery import DiscoveredJob, discover_jobs
from autoeditor.footage_only.inventory import build_inventory, write_inventory
from autoeditor.footage_only.normalize import load_manifest, normalize_clips
from autoeditor.footage_only.scenes import Scene, build_scenes, scenes_from_doc
from autoeditor.footage_only.shot_plan import generate_shot_plan
from autoeditor.footage_only.timeline import build_timeline
from autoeditor.footage_only.vision import analyze_scenes
from autoeditor.logging_utils import configure_logging, get_logger
from autoeditor.media.ffprobe import ProbeError, audio_duration, probe
from autoeditor.pipeline import render as render_mod
from autoeditor.pipeline.audio_assets import select_music, select_sfx
from autoeditor.pipeline.captions import build_captions
from autoeditor.pipeline.credits import SourceCredit, write_credits
from autoeditor.pipeline.job import JobPaths, read_json, write_json
from autoeditor.pipeline.metadata import build_metadata
from autoeditor.pipeline.qc import finalize_output, move_to_review, run_qc
from autoeditor.pipeline.state import JobState, StageError
from autoeditor.pipeline.upload import upload_allowed, upload_to_youtube
from autoeditor.pipeline.voice import LineTiming, build_voice, load_voice_timing
from autoeditor.providers.factory import build_providers
from autoeditor.providers.mock import MockTTS

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
    status: str  # complete | needs_review | skipped_render | dry_run | failed
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

    usable = 0
    total = 0.0
    for clip in job.clips:
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
    log.info("  usable clips: %d, total footage: %.1fs, topic: %s", usable, total, job.topic or "<infer>")
    for c in job.credits:
        log.info("  license %s: %s", c.file, c.license)
    state = JobState.load_or_create(paths.state_json, job.name, "footage_only") if paths.state_json.exists() else None
    done = state.completed_stages() if state else []
    log.info("  stages already complete: %s", ", ".join(done) or "none")
    log.info("  would write: %s, %s, %s", paths.work, paths.output, paths.cache)
    return JobResult(name=job.name, status="dry_run", message=f"{usable} usable clip(s), {total:.1f}s footage")


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

    providers = build_providers(
        cfg,
        need_llm=True,
        need_vision=not opts.skip_vision,
        need_tts=not opts.skip_voice,
        need_transcriber=not opts.skip_voice,
    )
    vision_cache = JsonCache(paths.cache / "vision")
    llm_cache = JsonCache(paths.cache / "llm")
    credits: list[SourceCredit] = list(job.credits)

    # Phase 1 -------------------------------------------------------------
    def discovered() -> dict[str, Any]:
        doc = job.to_dict()
        write_json(paths.work / "discovery.json", doc)
        return doc

    state.run_stage("discovered", discovered, skip_if_done=lambda: read_json(paths.work / "discovery.json"))

    # Phase 2 -------------------------------------------------------------
    clips = state.run_stage(
        "normalized",
        lambda: normalize_clips(job, paths, cfg),
        skip_if_done=lambda: load_manifest(paths),
    )
    if not any(c.usable for c in clips):
        raise StageError("normalized", RuntimeError("no usable clips after validation"))

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

    scenes_doc, scenes, inventory = state.run_stage("analyzed", analyze, skip_if_done=load_analysis)

    # Phase 6 -------------------------------------------------------------
    script = state.run_stage(
        "scripted",
        lambda: generate_shot_plan(inventory, job.topic, providers.llm, cfg, paths, cache=llm_cache),
        skip_if_done=lambda: read_json(paths.script_json),
    )

    # Phase 7 -------------------------------------------------------------
    def voiced() -> list[LineTiming]:
        tts = providers.tts
        if opts.skip_voice:
            state.add_warning("--skip-voice: placeholder tone track used instead of narration")
            tts = MockTTS(words_per_second=float(cfg.get("script.words_per_second", 2.6)))
        return build_voice(script, paths, tts, cfg)

    timings = state.run_stage("voiced", voiced, skip_if_done=lambda: load_voice_timing(paths))
    voice_duration = audio_duration(paths.voice_audio)

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
        tl = build_timeline(script, timings, scenes, inventory, paths, cfg, voice_duration=voice_duration, music=music, sfx=sfx)
        # Credit only footage that actually appears in the video.
        used_scene_ids = {seg.get("scene_id") for line in tl["lines"] for seg in line["segments"]}
        used_files = {s.source_file for s in scenes if s.scene_id in used_scene_ids}
        footage_credits = [c for c in credits if c.file in used_files]
        all_credits = footage_credits + ([music_credit] if music_credit else []) + sfx_credits
        write_credits(paths, script["title"], all_credits)
        best = _best_frame(scenes, inventory, paths)
        build_metadata(script, paths, providers.llm, cfg, thumbnail_source=best)
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

    try:
        state.run_stage("qc_passed", qc, skip_if_done=lambda: read_json(paths.qc_json))
    except StageError as exc:
        state.finish("needs_review")
        return JobResult(name=job.name, status="needs_review", final=final, message=str(exc.cause))

    # Upload (never automatic) ------------------------------------------------
    allowed, why = upload_allowed(cfg, requested=opts.upload, qc_passed=True)
    if allowed:
        metadata = read_json(paths.metadata_json)
        video_id = upload_to_youtube(final, metadata)
        state.mark("complete", uploaded=video_id)
    else:
        log.info("Upload skipped: %s", why)
        state.mark("complete", uploaded=None)
    state.finish("complete")
    log.info("Job %s complete -> %s", job.name, final)
    return JobResult(name=job.name, status="complete", final=final)


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
