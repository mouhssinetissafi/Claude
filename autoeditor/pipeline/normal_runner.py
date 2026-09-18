"""Normal mode orchestrator: topic/facts -> script -> voice -> captions -> media -> Remotion -> QC."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoeditor.cache import JsonCache
from autoeditor.config import Config
from autoeditor.logging_utils import configure_logging, get_logger
from autoeditor.media.ffprobe import audio_duration
from autoeditor.pipeline import render as render_mod
from autoeditor.pipeline.audio_assets import select_music, select_sfx
from autoeditor.pipeline.captions import build_captions
from autoeditor.pipeline.credits import SourceCredit, write_credits
from autoeditor.pipeline.job import JobPaths, read_json, sanitize_job_name
from autoeditor.pipeline.media_fetch import load_media, resolve_media
from autoeditor.pipeline.metadata import build_metadata
from autoeditor.pipeline.normal_timeline import build_normal_timeline
from autoeditor.pipeline.qc import finalize_output, move_to_review, run_qc
from autoeditor.pipeline.script_writer import read_facts, write_script
from autoeditor.pipeline.state import JobState, StageError
from autoeditor.pipeline.upload import upload_allowed, upload_to_youtube
from autoeditor.pipeline.voice import build_voice, load_voice_timing
from autoeditor.providers.factory import build_providers

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
    providers = build_providers(cfg, need_vision=False, need_tts=not opts.skip_voice, need_transcriber=not opts.skip_voice)
    facts = read_facts(opts.facts_file.read_text(encoding="utf-8")) if opts.facts_file and opts.facts_file.exists() else []
    llm_cache = JsonCache(paths.cache / "llm")

    state.run_stage("discovered", lambda: None, skip_if_done=lambda: None)
    script = state.run_stage(
        "scripted", lambda: write_script(opts.topic, facts, providers.llm, cfg, paths, cache=llm_cache), skip_if_done=lambda: read_json(paths.script_json)
    )

    def voiced() -> Any:
        tts = providers.tts
        if opts.skip_voice:
            from autoeditor.providers.mock import MockTTS

            state.add_warning("--skip-voice: placeholder tone track used")
            tts = MockTTS(words_per_second=float(cfg.get("script.words_per_second", 2.6)))
        return build_voice(script, paths, tts, cfg)

    timings = state.run_stage("voiced", voiced, skip_if_done=lambda: load_voice_timing(paths))
    voice_duration = audio_duration(paths.voice_audio)
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
        tl = build_normal_timeline(script, timings, media, paths, cfg, voice_duration=voice_duration, music=music, sfx=sfx)
        credits = [SourceCredit(**m.credit) for m in media] + ([music_credit] if music_credit else []) + sfx_credits
        write_credits(paths, script["title"], credits)
        first = next((paths.work / m.src for m in media if m.type == "image"), None)
        build_metadata(script, paths, providers.llm, cfg, thumbnail_source=first)
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

    try:
        state.run_stage("qc_passed", qc, skip_if_done=lambda: read_json(paths.qc_json))
    except StageError:
        state.finish("needs_review")
        return "needs_review"
    allowed, why = upload_allowed(cfg, requested=opts.upload, qc_passed=True)
    if allowed:
        state.mark("complete", uploaded=upload_to_youtube(final, read_json(paths.metadata_json)))
    else:
        log.info("Upload skipped: %s", why)
        state.mark("complete", uploaded=None)
    state.finish("complete")
    log.info("Normal mode complete -> %s", final)
    return "complete"
