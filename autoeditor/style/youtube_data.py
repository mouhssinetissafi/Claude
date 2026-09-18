"""YouTube Data API v3 client for public channel/video statistics.

Only public data is used: uploads, publish dates, durations, view/like/comment
counts. Retention and true 28-day analytics are owner-only and are never
estimated. The key comes from ``YOUTUBE_API_KEY`` and is never logged.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any, Protocol

from autoeditor.logging_utils import get_logger
from autoeditor.style.models import ChannelStats, VideoStats

log = get_logger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
_DURATION_RE = re.compile(r"P(?:(?P<d>\d+)D)?T?(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?")


class ReferenceFetchError(RuntimeError):
    pass


def parse_iso8601_duration(value: str) -> float:
    m = _DURATION_RE.fullmatch(value or "")
    if not m:
        raise ValueError(f"unrecognised duration {value!r}")
    parts = {k: int(v) if v else 0 for k, v in m.groupdict().items()}
    return float(parts["d"] * 86400 + parts["h"] * 3600 + parts["m"] * 60 + parts["s"])


class StatsSource(Protocol):
    name: str

    def fetch_channel(self, handle: str, *, sample_size: int, shorts_max_seconds: float) -> ChannelStats: ...


class YouTubeDataClient:
    """Thin urllib client. ~3 quota units per channel refresh."""

    name = "youtube_data_api"

    def __init__(self, api_key: str | None = None, *, timeout: float = 30.0) -> None:
        key = (api_key or os.environ.get("YOUTUBE_API_KEY", "")).strip()
        if not key:
            raise ReferenceFetchError("YOUTUBE_API_KEY is not set; cannot fetch reference statistics")
        self._key = key
        self.timeout = timeout

    def _get(self, resource: str, **params: Any) -> dict[str, Any]:
        params["key"] = self._key
        url = f"{API_BASE}/{resource}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            raise ReferenceFetchError(f"YouTube API {resource} HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ReferenceFetchError(f"YouTube API {resource} failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise ReferenceFetchError(f"YouTube API {resource} returned a non-object")
        return payload

    def fetch_channel(self, handle: str, *, sample_size: int, shorts_max_seconds: float) -> ChannelStats:
        handle_param = handle if handle.startswith("@") else f"@{handle}"
        channels = self._get("channels", part="contentDetails,snippet", forHandle=handle_param)
        items = channels.get("items") or []
        if not items:
            raise ReferenceFetchError(f"channel {handle_param} not found")
        channel_id = str(items[0]["id"])
        uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        playlist = self._get("playlistItems", part="contentDetails", playlistId=uploads, maxResults=min(50, max(1, sample_size)))
        video_ids = [str(it["contentDetails"]["videoId"]) for it in playlist.get("items", [])][:sample_size]
        videos: list[VideoStats] = []
        for i in range(0, len(video_ids), 50):
            batch = self._get("videos", part="contentDetails,statistics,snippet", id=",".join(video_ids[i : i + 50]))
            for it in batch.get("items", []):
                videos.append(_video_from_item(it, shorts_max_seconds))
        return ChannelStats(handle=handle, channel_id=channel_id, fetched_at=datetime.now(UTC).isoformat(timespec="seconds"), videos=videos, source=self.name)


def _video_from_item(item: dict[str, Any], shorts_max_seconds: float) -> VideoStats:
    stats = item.get("statistics") or {}
    snippet = item.get("snippet") or {}
    duration = parse_iso8601_duration(str((item.get("contentDetails") or {}).get("duration", "PT0S")))

    def _int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return VideoStats(
        video_id=str(item.get("id", "")),
        title=str(snippet.get("title", "")),
        published_at=str(snippet.get("publishedAt", "")),
        duration_seconds=duration,
        views=_int(stats.get("viewCount")) or 0,
        likes=_int(stats.get("likeCount")),
        comments=_int(stats.get("commentCount")),
        is_short=0 < duration <= shorts_max_seconds,
    )


class ManualImportSource:
    """Statistics supplied by hand (JSON export) for operators without an API key.

    File shape: {"channels": [{"handle": ..., "channel_id": ..., "fetched_at": ..., "videos": [VideoStats...]}]}
    """

    name = "manual_import"

    def __init__(self, data: dict[str, Any]) -> None:
        self._by_handle = {str(c["handle"]).lower(): c for c in data.get("channels", [])}

    def fetch_channel(self, handle: str, *, sample_size: int, shorts_max_seconds: float) -> ChannelStats:
        raw = self._by_handle.get(handle.lower())
        if raw is None:
            raise ReferenceFetchError(f"no manual statistics for {handle}")
        stats = ChannelStats.from_dict(raw)
        stats.source = self.name
        for v in stats.videos:
            v.is_short = 0 < v.duration_seconds <= shorts_max_seconds
        stats.videos = stats.videos[:sample_size]
        return stats
