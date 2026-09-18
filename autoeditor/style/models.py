"""Data models for reference channels, fetched statistics and the style profile."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

TIER_HIGH = "high"
TIER_MEDIUM = "medium"
TIER_LOW = "low"
TIER_UNVERIFIED = "unverified"  # no usable statistics yet (or stale)


@dataclass
class ReferenceChannel:
    handle: str
    url: str = ""
    role: str = "style"  # style | research
    trust: str = "unverified"  # user_verified_strong | unverified
    notes: str = ""

    @property
    def key(self) -> str:
        return self.handle.lower()


@dataclass
class VideoStats:
    video_id: str
    title: str
    published_at: str  # ISO 8601
    duration_seconds: float
    views: int
    likes: int | None = None
    comments: int | None = None
    is_short: bool = True
    # Derived (filled by analysis)
    hours_since_publish: float | None = None
    views_per_hour: float | None = None
    outlier_score: float | None = None
    engagement_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VideoStats:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ChannelStats:
    handle: str
    channel_id: str
    fetched_at: str
    videos: list[VideoStats] = field(default_factory=list)  # latest sample, Shorts and long-form
    source: str = "youtube_data_api"  # youtube_data_api | manual_import | mock
    error: str | None = None

    @property
    def shorts(self) -> list[VideoStats]:
        return [v for v in self.videos if v.is_short]

    @property
    def long_form(self) -> list[VideoStats]:
        return [v for v in self.videos if not v.is_short]

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "channel_id": self.channel_id,
            "fetched_at": self.fetched_at,
            "source": self.source,
            "error": self.error,
            "videos": [v.to_dict() for v in self.videos],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChannelStats:
        return cls(
            handle=str(data["handle"]),
            channel_id=str(data.get("channel_id", "")),
            fetched_at=str(data.get("fetched_at", "")),
            source=str(data.get("source", "manual_import")),
            error=data.get("error"),
            videos=[VideoStats.from_dict(v) for v in data.get("videos", [])],
        )


@dataclass
class PerformanceSignals:
    """Everything derivable from public per-video statistics. Retention is not public and is never estimated."""

    shorts_in_sample: int = 0
    long_form_in_sample: int = 0
    median_views: float = 0.0
    views_28d_lower_bound: int = 0  # views on Shorts published within the window (public lower bound of 28-day views)
    viral_count: int = 0  # Shorts >= viral_video_views
    outlier_count: int = 0  # Shorts >= outlier_ratio x median
    max_views: int = 0
    median_views_per_hour: float = 0.0
    median_engagement_rate: float | None = None
    shorts_vs_long_form_ratio: float | None = None  # median Shorts views / median long-form views
    signals_met: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChannelEvaluation:
    handle: str
    role: str
    trust: str
    tier: str
    weight: float
    reason: str
    signals: PerformanceSignals | None = None
    stats_age_days: float | None = None
    top_videos: list[dict[str, Any]] = field(default_factory=list)
    video_level_findings: list[str] = field(default_factory=list)
    active: bool = False  # part of the active style profile

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["signals"] = self.signals.to_dict() if self.signals else None
        return d
