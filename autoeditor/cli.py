"""Command-line interface (Phase 14).

python run.py --topic "why the iPhone Air is thin"          # normal mode
python run.py --footage-only --inbox ./inbox                 # footage-only mode
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from autoeditor import __version__
from autoeditor.config import ConfigError, load_config
from autoeditor.logging_utils import configure_logging, get_logger
from autoeditor.providers.base import MissingCredentialsError, ProviderError

log = get_logger("autoeditor")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run.py", description="Auto-Editor PRO - automated YouTube Shorts")
    p.add_argument("--version", action="version", version=f"Auto-Editor PRO {__version__}")
    p.add_argument("--config", type=Path, default=None, help="YAML file merged over config/default.yaml")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--mock", action="store_true", help="Use deterministic offline providers (no API calls, no cost)")
    p.add_argument("--dry-run", action="store_true", help="Inspect inputs and report the plan; no encoding, no API calls")
    p.add_argument("--job", default=None, help="Only process this job name")
    p.add_argument("--skip-voice", action="store_true", help="Use a placeholder tone instead of TTS")
    p.add_argument("--skip-render", action="store_true", help="Stop after timeline.json (no Remotion)")
    p.add_argument("--no-upload", action="store_true", help="Never upload (default; kept for explicitness)")
    p.add_argument("--upload", action="store_true", help="Upload after QC passes (requires upload.enabled and credentials)")
    p.add_argument("--max-duration", type=float, default=None, help="Target maximum narration seconds (script.target_max_seconds)")
    p.add_argument("--theme", default=None, help="Remotion theme name (render.theme)")

    mode = p.add_argument_group("normal mode")
    mode.add_argument("--topic", default=None, help="Topic to write about")
    mode.add_argument("--topic-file", type=Path, default=None, help="File whose contents are the topic")
    mode.add_argument("--facts", type=Path, default=None, help="Text file of verified facts (one per line)")

    fo = p.add_argument_group("footage-only mode")
    fo.add_argument("--footage-only", action="store_true", help="Build Shorts from raw clips in the inbox")
    fo.add_argument("--inbox", type=Path, default=None, help="Inbox folder (default: config project.inbox_dir)")
    fo.add_argument("--skip-vision", action="store_true", help="Skip vision analysis (neutral scores, no API cost)")
    fo.add_argument("--force-reanalyze", action="store_true", help="Ignore vision/LLM caches and re-run analysis")
    fo.add_argument("--min-scene-duration", type=float, default=None, help="Minimum scene seconds (media.min_scene_seconds)")
    fo.add_argument("--min-clip-duration", type=float, default=None, help="Minimum clip seconds (media.min_clip_seconds)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    overrides = {
        "script.target_max_seconds": args.max_duration,
        "media.min_scene_seconds": args.min_scene_duration,
        "media.min_clip_seconds": args.min_clip_duration,
        "render.theme": args.theme,
    }
    try:
        cfg = load_config(args.config, overrides, mock=True if args.mock else None)
    except ConfigError as exc:
        log.error("Config error: %s", exc)
        return 2
    if args.upload and args.no_upload:
        log.error("--upload and --no-upload are mutually exclusive")
        return 2
    if cfg.mock:
        log.info("Mock mode: no external APIs will be called")

    try:
        if args.footage_only:
            return _run_footage_only(cfg, args)
        return _run_normal(cfg, args)
    except MissingCredentialsError as exc:
        log.error("%s", exc)
        return 3
    except ProviderError as exc:
        log.error("Provider error: %s", exc)
        return 4
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 2


def _run_footage_only(cfg, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    from autoeditor.footage_only.runner import FootageOnlyOptions, run_footage_only

    inbox = args.inbox or (cfg.root / str(cfg.get("project.inbox_dir", "inbox")))
    opts = FootageOnlyOptions(
        inbox=inbox,
        job=args.job,
        skip_vision=args.skip_vision,
        skip_voice=args.skip_voice,
        skip_render=args.skip_render,
        force_reanalyze=args.force_reanalyze,
        dry_run=args.dry_run,
        upload=bool(args.upload and not args.no_upload),
    )
    results = run_footage_only(cfg, opts)
    if not results:
        return 1
    log.info("---- summary ----")
    exit_code = 0
    for r in results:
        log.info("%-24s %-16s %s", r.name, r.status, r.final or r.message)
        if r.status == "awaiting_review":
            log.info("%-24s next: watch the video, then create APPROVED next to it (see REVIEW.md) before any upload", "")
        if r.status in {"failed", "needs_review"}:
            exit_code = 1
    return exit_code


def _run_normal(cfg, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    from autoeditor.pipeline.normal_runner import NormalOptions, run_normal

    topic = args.topic
    if not topic and args.topic_file and args.topic_file.exists():
        topic = args.topic_file.read_text(encoding="utf-8").strip()
    if not topic:
        log.error("Normal mode needs --topic or --topic-file (or use --footage-only --inbox ./inbox)")
        return 2
    opts = NormalOptions(
        topic=topic,
        job=args.job,
        facts_file=args.facts,
        skip_voice=args.skip_voice,
        skip_render=args.skip_render,
        dry_run=args.dry_run,
        upload=bool(args.upload and not args.no_upload),
    )
    status = run_normal(cfg, opts)
    log.info("normal mode: %s", status)
    return 0 if status in {"complete", "awaiting_review", "skipped_render", "dry_run"} else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
