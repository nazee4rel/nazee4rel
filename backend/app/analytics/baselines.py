"""Robust baselines and anomaly detection.

Everything here uses the median and MAD rather than the mean and standard
deviation. That is not fastidiousness: a single viral post is exactly the event
this system exists to notice, and it would drag a mean-based baseline upward for
weeks afterwards — so the very success you want flagged would quietly raise the
bar and suppress the next flag. The median barely moves.

The account is always compared against itself. There is no global benchmark
here, because "good growth" for a 500-follower account and a 500,000-follower
account have nothing in common.
"""

from __future__ import annotations

import enum
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime

# Converts MAD to a standard-deviation-equivalent for normally distributed data.
MAD_TO_SIGMA = 1.4826

# Below this, a "baseline" is just noise wearing a suit.
MIN_OBSERVATIONS = 7
# Enough to see weekly rhythm without letting ancient history dominate.
DEFAULT_WINDOW = 28


class AnomalyDirection(enum.StrEnum):
    SPIKE = "SPIKE"
    DROP = "DROP"
    NORMAL = "NORMAL"


class AnomalySeverity(enum.StrEnum):
    NONE = "NONE"
    MODERATE = "MODERATE"  # beyond ~3 robust sigma
    STRONG = "STRONG"  # beyond ~5
    EXTREME = "EXTREME"  # beyond ~8


@dataclass(frozen=True)
class Baseline:
    median: float
    mad: float
    robust_sigma: float
    observations: int
    is_reliable: bool
    note: str = ""

    def z_score(self, value: float) -> float | None:
        """Robust z-score. None when the baseline cannot support one."""
        if not self.is_reliable:
            return None
        if self.robust_sigma == 0:
            # A perfectly flat history. Any deviation is notable, but a z-score
            # would be infinite, so report None and let the caller decide.
            return None if value == self.median else None
        return (value - self.median) / self.robust_sigma


@dataclass(frozen=True)
class Anomaly:
    value: float
    direction: AnomalyDirection
    severity: AnomalySeverity
    z_score: float | None
    baseline: Baseline
    explanation: str

    @property
    def is_anomalous(self) -> bool:
        return self.direction is not AnomalyDirection.NORMAL


def compute_baseline(values: list[float], window: int = DEFAULT_WINDOW) -> Baseline:
    """Median and MAD over the most recent `window` observations."""
    recent = values[-window:] if window > 0 else list(values)
    count = len(recent)

    if count < MIN_OBSERVATIONS:
        median = statistics.median(recent) if recent else 0.0
        return Baseline(
            median=median,
            mad=0.0,
            robust_sigma=0.0,
            observations=count,
            is_reliable=False,
            note=(
                f"Only {count} observation(s); at least {MIN_OBSERVATIONS} are needed "
                f"before a baseline means anything."
            ),
        )

    median = statistics.median(recent)
    mad = statistics.median([abs(v - median) for v in recent])
    return Baseline(
        median=median,
        mad=mad,
        robust_sigma=mad * MAD_TO_SIGMA,
        observations=count,
        is_reliable=True,
    )


def detect_anomaly(
    value: float,
    baseline: Baseline,
    *,
    moderate: float = 3.0,
    strong: float = 5.0,
    extreme: float = 8.0,
) -> Anomaly:
    """Classify one observation against a baseline."""
    if not baseline.is_reliable:
        return Anomaly(
            value=value,
            direction=AnomalyDirection.NORMAL,
            severity=AnomalySeverity.NONE,
            z_score=None,
            baseline=baseline,
            explanation=baseline.note or "Baseline is not yet reliable.",
        )

    z = baseline.z_score(value)

    if z is None:
        # Zero spread: the history is perfectly flat. Treat a departure as
        # notable but unquantified rather than inventing a z-score.
        if value == baseline.median:
            return Anomaly(
                value,
                AnomalyDirection.NORMAL,
                AnomalySeverity.NONE,
                None,
                baseline,
                "Exactly at baseline.",
            )
        direction = AnomalyDirection.SPIKE if value > baseline.median else AnomalyDirection.DROP
        return Anomaly(
            value,
            direction,
            AnomalySeverity.MODERATE,
            None,
            baseline,
            f"History was perfectly flat at {baseline.median:g}; this reading of "
            f"{value:g} departs from it, though the spread gives no scale to judge by.",
        )

    magnitude = abs(z)
    if magnitude < moderate:
        return Anomaly(
            value,
            AnomalyDirection.NORMAL,
            AnomalySeverity.NONE,
            z,
            baseline,
            f"Within normal range ({z:+.1f} robust sigma).",
        )

    direction = AnomalyDirection.SPIKE if z > 0 else AnomalyDirection.DROP
    severity = (
        AnomalySeverity.EXTREME
        if magnitude >= extreme
        else AnomalySeverity.STRONG
        if magnitude >= strong
        else AnomalySeverity.MODERATE
    )
    word = "above" if direction is AnomalyDirection.SPIKE else "below"
    return Anomaly(
        value,
        direction,
        severity,
        z,
        baseline,
        f"{value:g} is {magnitude:.1f} robust sigma {word} the usual {baseline.median:g}.",
    )


@dataclass(frozen=True)
class SeasonalProfile:
    """Median value per weekday, for accounts with a weekly rhythm."""

    by_weekday: dict[int, float] = field(default_factory=dict)
    observations_by_weekday: dict[int, int] = field(default_factory=dict)
    is_reliable: bool = False

    def expected_for(self, when: datetime) -> float | None:
        if not self.is_reliable:
            return None
        return self.by_weekday.get(when.weekday())


# Two observations for a weekday is a coincidence, not a pattern.
MIN_PER_WEEKDAY = 3


def compute_seasonality(observations: list[tuple[datetime, float]]) -> SeasonalProfile:
    """Weekday medians, gated on having enough data per weekday.

    Without the gate a single Tuesday would define "typical Tuesday" and every
    subsequent Tuesday would look anomalous against it.
    """
    buckets: dict[int, list[float]] = {}
    for when, value in observations:
        weekday = (when if when.tzinfo else when.replace(tzinfo=UTC)).weekday()
        buckets.setdefault(weekday, []).append(value)

    by_weekday = {
        day: statistics.median(values)
        for day, values in buckets.items()
        if len(values) >= MIN_PER_WEEKDAY
    }
    counts = {day: len(values) for day, values in buckets.items()}

    return SeasonalProfile(
        by_weekday=by_weekday,
        observations_by_weekday=counts,
        # At least four weekdays covered before the profile is usable at all.
        is_reliable=len(by_weekday) >= 4,
    )


@dataclass(frozen=True)
class TrendResult:
    direction: str  # "rising" | "falling" | "flat"
    change_ratio: float | None
    recent_median: float
    prior_median: float
    is_reliable: bool
    explanation: str


def compare_periods(
    recent: list[float], prior: list[float], *, threshold: float = 0.1
) -> TrendResult:
    """Compare two periods using medians rather than totals.

    Totals are dominated by outliers, which is precisely wrong when the question
    is "has my typical performance changed?" — one viral post should not make a
    declining account look healthy.
    """
    if len(recent) < 3 or len(prior) < 3:
        return TrendResult(
            "flat",
            None,
            statistics.median(recent) if recent else 0.0,
            statistics.median(prior) if prior else 0.0,
            False,
            "Not enough observations in one or both periods to compare.",
        )

    recent_median = statistics.median(recent)
    prior_median = statistics.median(prior)

    if prior_median == 0:
        return TrendResult(
            "rising" if recent_median > 0 else "flat",
            None,
            recent_median,
            prior_median,
            True,
            "The earlier period had a median of zero, so no ratio is meaningful.",
        )

    ratio = (recent_median - prior_median) / prior_median
    direction = "rising" if ratio > threshold else "falling" if ratio < -threshold else "flat"
    return TrendResult(
        direction,
        ratio,
        recent_median,
        prior_median,
        True,
        f"Median moved from {prior_median:g} to {recent_median:g} ({ratio:+.1%}).",
    )
