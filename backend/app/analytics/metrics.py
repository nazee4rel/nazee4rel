"""Engagement metrics.

Every rate here returns the denominator it used, because "engagement rate" is
not one number. Rate-per-impression and rate-per-follower can tell opposite
stories about the same post — a post shown to few people can have superb
per-impression engagement and negligible per-follower engagement — and quoting
one without saying which is how analytics dashboards mislead.

Impressions are preferred where available. They are not always available: the
X API only returns them for posts under 30 days old, so for older posts the
per-follower fallback is all that exists, and the result says so.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.models.enums import Provenance


class Denominator(enum.StrEnum):
    IMPRESSIONS = "IMPRESSIONS"  # preferred: engagement per person reached
    FOLLOWERS = "FOLLOWERS"  # fallback: engagement per potential viewer
    NONE = "NONE"  # neither available — the rate is undefined


@dataclass(frozen=True)
class EngagementRate:
    """A rate, its denominator, and how much to trust it."""

    value: float | None
    denominator: Denominator
    numerator: int
    denominator_value: int | None
    provenance: Provenance

    @property
    def is_available(self) -> bool:
        return self.value is not None

    @property
    def percent(self) -> float | None:
        return None if self.value is None else self.value * 100

    def describe(self) -> str:
        if self.value is None:
            return "Not computable — neither impressions nor a follower count were available."
        per = "impression" if self.denominator is Denominator.IMPRESSIONS else "follower"
        return f"{self.percent:.2f}% per {per}"


# Bookmarks and quotes are weighted above likes: they take more effort and
# correlate better with reach. This is a judgement call, so it is exposed as a
# named constant rather than buried in a formula.
ENGAGEMENT_WEIGHTS = {
    "like_count": 1.0,
    "reply_count": 2.0,
    "retweet_count": 3.0,
    "quote_count": 3.0,
    "bookmark_count": 2.0,
}


def total_engagement(
    like_count: int = 0,
    reply_count: int = 0,
    retweet_count: int = 0,
    quote_count: int = 0,
    bookmark_count: int = 0,
) -> int:
    """Unweighted sum of engagement actions."""
    return like_count + reply_count + retweet_count + quote_count + bookmark_count


def weighted_engagement(
    like_count: int = 0,
    reply_count: int = 0,
    retweet_count: int = 0,
    quote_count: int = 0,
    bookmark_count: int = 0,
) -> float:
    """Engagement weighted by effort, per `ENGAGEMENT_WEIGHTS`."""
    return (
        like_count * ENGAGEMENT_WEIGHTS["like_count"]
        + reply_count * ENGAGEMENT_WEIGHTS["reply_count"]
        + retweet_count * ENGAGEMENT_WEIGHTS["retweet_count"]
        + quote_count * ENGAGEMENT_WEIGHTS["quote_count"]
        + bookmark_count * ENGAGEMENT_WEIGHTS["bookmark_count"]
    )


def engagement_rate(
    engagement: int,
    *,
    impressions: int | None = None,
    followers: int | None = None,
) -> EngagementRate:
    """Compute an engagement rate, preferring impressions.

    A zero impression count is treated as no usable denominator rather than as
    a division by zero — a post with no impressions has an undefined rate, not
    an infinite one.
    """
    if impressions:
        return EngagementRate(
            value=engagement / impressions,
            denominator=Denominator.IMPRESSIONS,
            numerator=engagement,
            denominator_value=impressions,
            provenance=Provenance.DERIVED,
        )

    if followers:
        return EngagementRate(
            value=engagement / followers,
            denominator=Denominator.FOLLOWERS,
            numerator=engagement,
            denominator_value=followers,
            provenance=Provenance.DERIVED,
        )

    return EngagementRate(
        value=None,
        denominator=Denominator.NONE,
        numerator=engagement,
        denominator_value=None,
        # Not DERIVED: nothing was derived. The value is unavailable, which the
        # UI must render as a gap rather than as zero.
        provenance=Provenance.UNAVAILABLE,
    )


@dataclass(frozen=True)
class VelocityPoint:
    age_hours: float
    engagement: int
    impressions: int | None


def engagement_velocity(points: list[VelocityPoint]) -> list[tuple[float, float]]:
    """Engagement gained per hour between consecutive snapshots.

    This is what the decaying snapshot cadence exists to capture, and what makes
    a breakout distinguishable from a post that merely accumulated slowly.
    """
    ordered = sorted(points, key=lambda p: p.age_hours)
    velocity: list[tuple[float, float]] = []

    for previous, current in zip(ordered, ordered[1:], strict=False):
        elapsed = current.age_hours - previous.age_hours
        if elapsed <= 0:
            continue
        gained = current.engagement - previous.engagement
        # Counters should be monotonic, but X occasionally revises them
        # downwards (deleted replies, spam removal). Clamp rather than
        # reporting negative velocity.
        velocity.append((current.age_hours, max(0.0, gained) / elapsed))

    return velocity


def peak_velocity(points: list[VelocityPoint]) -> tuple[float, float] | None:
    """The (age_hours, rate) at which a post gained engagement fastest."""
    velocity = engagement_velocity(points)
    return max(velocity, key=lambda item: item[1]) if velocity else None


def percentile_rank(value: float, population: list[float]) -> float | None:
    """Where `value` sits in `population`, 0-100.

    Compared against the account's own trailing distribution rather than any
    global benchmark: "good engagement" only means anything relative to what
    this account normally achieves.
    """
    if not population:
        return None
    below = sum(1 for item in population if item < value)
    equal = sum(1 for item in population if item == value)
    # Midpoint convention, so identical values do not all rank at the bottom.
    return 100.0 * (below + 0.5 * equal) / len(population)


def rpm(revenue_minor: int, impressions: int | None) -> float | None:
    """Revenue per 1,000 impressions, in minor currency units.

    Returns None rather than 0 when impressions are unknown. An RPM computed
    against a missing denominator would be fiction, and this metric spans
    user-entered revenue and measured impressions, so it is doubly worth
    refusing to guess.
    """
    if not impressions:
        return None
    return revenue_minor * 1000 / impressions
