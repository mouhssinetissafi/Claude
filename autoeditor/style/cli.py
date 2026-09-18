"""`python run.py --references <action>`: refresh statistics, report tiers, build the profile."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from autoeditor.config import Config
from autoeditor.logging_utils import get_logger
from autoeditor.style.models import ChannelStats
from autoeditor.style.profile import build_profile, channel_summary_rows, influence_lines, save_profile, stats_overview
from autoeditor.style.references import channels_from_config, evaluate_channels, load_references, load_stats, profile_path, save_stats, stats_path
from autoeditor.style.youtube_data import ManualImportSource, ReferenceFetchError, StatsSource, YouTubeDataClient

log = get_logger(__name__)

ACTIONS = ("refresh", "report", "profile", "import")


def refresh(cfg: Config, *, source: StatsSource | None = None, only: list[str] | None = None) -> dict[str, ChannelStats]:
    refs = load_references(cfg)
    perf = refs.get("performance", {})
    channels = channels_from_config(refs)
    existing = load_stats(cfg)
    client = source or YouTubeDataClient()
    wanted = {h.lower().lstrip("@") for h in only} if only else None
    for ch in channels:
        if wanted and ch.key not in wanted:
            continue
        try:
            stats = client.fetch_channel(ch.handle, sample_size=int(perf.get("sample_size", 20)), shorts_max_seconds=float(perf.get("shorts_max_seconds", 180)))
            existing[ch.key] = stats
            log.info("%s: %d videos fetched (%d Shorts)", ch.handle, len(stats.videos), len(stats.shorts))
        except ReferenceFetchError as exc:
            log.warning("%s: %s", ch.handle, exc)
            existing[ch.key] = ChannelStats(
                handle=ch.handle, channel_id="", fetched_at=datetime.now(UTC).isoformat(timespec="seconds"), videos=[], source=client.name, error=str(exc)
            )
    save_stats(cfg, existing)
    return existing


def import_stats(cfg: Config, path: Path) -> dict[str, ChannelStats]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return refresh(cfg, source=ManualImportSource(data))


def report(cfg: Config) -> list[str]:
    refs = load_references(cfg)
    stats = load_stats(cfg)
    evaluations = evaluate_channels(channels_from_config(refs), stats, refs.get("performance", {}))
    lines = [f"reference registry: {len(evaluations)} channel(s); statistics file: {stats_path(cfg)}"]
    lines += stats_overview(stats) or ["no statistics fetched yet - run: python run.py --references refresh   (needs YOUTUBE_API_KEY)"]
    lines.append("")
    lines += channel_summary_rows(evaluations)
    return lines


def run(action: str, cfg: Config, *, import_path: Path | None = None, only: list[str] | None = None) -> int:
    if action == "refresh":
        try:
            refresh(cfg, only=only)
        except ReferenceFetchError as exc:
            log.error("%s", exc)
            return 3
    elif action == "import":
        if import_path is None or not import_path.exists():
            log.error("--references import needs --references-file <stats.json>")
            return 2
        import_stats(cfg, import_path)
    if action in {"refresh", "import", "profile"}:
        profile = build_profile(cfg)
        path = save_profile(cfg, profile)
        log.info("style profile written: %s (%s)", path, profile["source"])
        for line in influence_lines(profile):
            log.info("  %s", line)
    if action == "report":
        for line in report(cfg):
            log.info("%s", line)
        prof = profile_path(cfg)
        log.info("style profile: %s", prof if prof.exists() else "not built yet (run --references profile)")
    return 0
