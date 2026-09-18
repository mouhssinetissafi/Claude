"""Short duration policy (minimum 45s, preferred 50-60s, avoid > 70s).

The policy is applied at three points: footage sufficiency before scripting,
estimated script length before voicing, and measured narration length after
voicing. Expansion asks the writer for *useful* context, never filler, and the
timeline never slows or needlessly repeats footage to fill time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from autoeditor.config import Config

_WORD = re.compile(r"[A-Za-z0-9']+")


@dataclass(frozen=True)
class DurationPolicy:
    min_final_seconds: float = 45.0
    target_min_seconds: float = 50.0
    target_max_seconds: float = 60.0
    hard_max_seconds: float = 70.0
    tolerance_seconds: float = 1.5
    words_per_second: float = 2.6
    max_expansions: int = 2
    outro_seconds: float = 0.6
    min_footage_coverage: float = 1.0

    @property
    def target_seconds(self) -> float:
        return (self.target_min_seconds + self.target_max_seconds) / 2

    @property
    def min_footage_seconds(self) -> float:
        return self.min_final_seconds * self.min_footage_coverage


def policy_from_config(cfg: Config) -> DurationPolicy:
    return DurationPolicy(
        min_final_seconds=float(cfg.get("script.min_final_seconds", 45)),
        target_min_seconds=float(cfg.get("script.target_min_seconds", 50)),
        target_max_seconds=float(cfg.get("script.target_max_seconds", 60)),
        hard_max_seconds=float(cfg.get("script.hard_max_seconds", 70)),
        tolerance_seconds=float(cfg.get("script.duration_tolerance_seconds", 1.5)),
        words_per_second=float(cfg.get("script.words_per_second", 2.6)),
        max_expansions=int(cfg.get("script.max_expansions", 2)),
        outro_seconds=float(cfg.get("timeline.outro_seconds", 0.6)),
        min_footage_coverage=float(cfg.get("script.min_footage_coverage", 1.0)),
    )


def estimated_seconds(lines: list[dict[str, Any]], words_per_second: float) -> float:
    words = sum(len(_WORD.findall(str(line.get("narration", "")))) for line in lines)
    return round(words / words_per_second, 2) if words_per_second > 0 else 0.0


def narration_shortfall(seconds: float, policy: DurationPolicy) -> float:
    """Seconds missing to reach the minimum (0 when the minimum is met within tolerance)."""
    missing = policy.min_final_seconds - seconds
    return round(missing, 2) if missing > policy.tolerance_seconds else 0.0


def expansion_target(seconds: float, policy: DurationPolicy) -> float:
    """How many seconds to add so the result lands inside the preferred range."""
    return round(max(policy.target_seconds - seconds, narration_shortfall(seconds, policy) + policy.tolerance_seconds), 1)


def footage_sufficient(total_usable_seconds: float, policy: DurationPolicy) -> tuple[bool, str]:
    need = policy.min_footage_seconds
    if total_usable_seconds + 0.01 >= need:
        return True, f"{total_usable_seconds:.1f}s usable footage covers the {policy.min_final_seconds:.0f}s minimum"
    return False, (
        f"insufficient footage: {total_usable_seconds:.1f}s usable, but a {policy.min_final_seconds:.0f}s Short needs at least "
        f"{need:.0f}s so no scene has to be slowed down or repeated. Add more clips."
    )


def expansion_instruction(current_seconds: float, add_seconds: float, policy: DurationPolicy) -> str:
    return (
        f"EXPANSION REQUIRED: the current narration runs about {current_seconds:.0f} seconds; the minimum is "
        f"{policy.min_final_seconds:.0f} seconds and the preferred length is {policy.target_min_seconds:.0f}-"
        f"{policy.target_max_seconds:.0f} seconds. Add roughly {add_seconds:.0f} seconds of narration "
        f"(about {int(add_seconds * policy.words_per_second)} words) of USEFUL context: a concrete detail visible in the footage, "
        "a contrast, a consequence, or a step the viewer would otherwise miss. Do not pad with filler, repetition, "
        "generic praise or restated lines. Keep the existing opening line as the opening and keep the ending as the ending; "
        "insert or extend lines in between. Return the COMPLETE revised script with every line."
    )
