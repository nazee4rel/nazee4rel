"""Posting-time and format analysis.

The failure mode this module is built against is confident nonsense: "post at
3am on Tuesdays" derived from two posts that happened to do well. Every result
here is gated on sample size, and a bucket that has not earned a conclusion says
so rather than producing one.

Times are computed in the account owner's timezone, not UTC. A recommendation to
post at 14:00 UTC is useless to someone who thinks in their own clock.
"""

from __future__ import annotations

import statistics
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime

# One post in a bucket is an anecdote. Three is the least that can carry a
# median worth acting on.
MIN_POSTS_PER_BUCKET = 3
# Below this the whole analysis is withheld rather than caveated.
MIN_TOTAL_POSTS = 15

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass(frozen=True)
class Bucket:
    """One (weekday, hour) slot, or one format group."""

    label: str
    sample_size: int
    median_performance: float
    is_reliable: bool

    @property
    def status(self) -> str:
        if self.is_reliable:
            return "reliable"
        return f"only {self.sample_size} post(s) — not enough to conclude anything"


@dataclass
class TimingAnalysis:
    timezone: str
    total_posts: int
    by_hour: list[Bucket] = field(default_factory=list)
    by_weekday: list[Bucket] = field(default_factory=list)
    by_slot: list[Bucket] = field(default_factory=list)
    best_hours: list[Bucket] = field(default_factory=list)
    worst_hours: list[Bucket] = field(default_factory=list)
    is_reliable: bool = False
    caveats: list[str] = field(default_factory=list)

    def recommendation(self) -> str:
        if not self.is_reliable:
            return "Not enough posting history to recommend times yet. " + (
                self.caveats[0] if self.caveats else ""
            )
        if not self.best_hours:
            return (
                "No hour has enough posts behind it to single out. Posting across "
                "more times of day would make this analysable."
            )
        top = ", ".join(b.label for b in self.best_hours[:3])
        return f"Best-performing hours in your timezone ({self.timezone}): {top}."


def _to_local(when: datetime, tz: zoneinfo.ZoneInfo) -> datetime:
    return when.astimezone(tz)


def _bucket(label: str, values: list[float]) -> Bucket:
    return Bucket(
        label=label,
        sample_size=len(values),
        median_performance=statistics.median(values) if values else 0.0,
        is_reliable=len(values) >= MIN_POSTS_PER_BUCKET,
    )


def analyse_timing(
    posts: list[tuple[datetime, float]], timezone_name: str = "UTC"
) -> TimingAnalysis:
    """Performance by hour, weekday and (weekday, hour) slot.

    `posts` is (posted_at, performance) where performance is whatever the caller
    considers meaningful — engagement rate, follower gain, impressions.
    """
    try:
        tz = zoneinfo.ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001
        tz = zoneinfo.ZoneInfo("UTC")
        timezone_name = "UTC"

    analysis = TimingAnalysis(timezone=timezone_name, total_posts=len(posts))

    if len(posts) < MIN_TOTAL_POSTS:
        analysis.caveats.append(
            f"Only {len(posts)} posts with performance data. At least "
            f"{MIN_TOTAL_POSTS} are needed before posting-time patterns can be "
            f"separated from chance."
        )
        return analysis

    hours: dict[int, list[float]] = {}
    weekdays: dict[int, list[float]] = {}
    slots: dict[tuple[int, int], list[float]] = {}

    for posted_at, performance in posts:
        local = _to_local(posted_at, tz)
        hours.setdefault(local.hour, []).append(performance)
        weekdays.setdefault(local.weekday(), []).append(performance)
        slots.setdefault((local.weekday(), local.hour), []).append(performance)

    analysis.by_hour = sorted(
        (_bucket(f"{hour:02d}:00", values) for hour, values in hours.items()),
        key=lambda b: b.label,
    )
    analysis.by_weekday = sorted(
        (_bucket(WEEKDAY_NAMES[day], values) for day, values in weekdays.items()),
        key=lambda b: WEEKDAY_NAMES.index(b.label),
    )
    analysis.by_slot = [
        _bucket(f"{WEEKDAY_NAMES[day]} {hour:02d}:00", values)
        for (day, hour), values in slots.items()
    ]

    # Only reliable buckets are eligible to be called best or worst — this is
    # the gate that stops "3am Tuesday" recommendations.
    reliable_hours = [b for b in analysis.by_hour if b.is_reliable]
    ranked = sorted(reliable_hours, key=lambda b: b.median_performance, reverse=True)
    analysis.best_hours = ranked[:5]
    analysis.worst_hours = ranked[-3:][::-1] if len(ranked) > 3 else []

    # Two well-sampled hours are enough for a genuine comparison. The guard
    # against noise is the per-bucket sample size, not the number of distinct
    # hours — demanding breadth as well would withhold a real finding from
    # someone who posts consistently at two times of day.
    analysis.is_reliable = len(reliable_hours) >= 2
    if not analysis.is_reliable:
        analysis.caveats.append(
            f"Only {len(reliable_hours)} hour(s) have {MIN_POSTS_PER_BUCKET}+ posts "
            f"behind them, so no hour can be singled out yet. Posting across more "
            f"times of day would make this analysable."
        )
    elif len(reliable_hours) == 2:
        analysis.caveats.append(
            "Only two times of day have enough posts to compare, so this says which "
            "of those two works better — not that either is the best available."
        )

    unreliable = len(analysis.by_hour) - len(reliable_hours)
    if unreliable:
        analysis.caveats.append(
            f"{unreliable} hour(s) are shown for completeness but have too few posts "
            f"to draw conclusions from."
        )

    return analysis


@dataclass
class FormatAnalysis:
    """Performance by deterministic post format.

    Formats are detected mechanically at collection time (media, link, thread,
    length, type), so unlike topics they need no model and carry no
    classification error.
    """

    groups: list[Bucket] = field(default_factory=list)
    total_posts: int = 0
    is_reliable: bool = False
    caveats: list[str] = field(default_factory=list)

    @property
    def best(self) -> Bucket | None:
        reliable = [g for g in self.groups if g.is_reliable]
        return max(reliable, key=lambda g: g.median_performance) if reliable else None

    @property
    def worst(self) -> Bucket | None:
        reliable = [g for g in self.groups if g.is_reliable]
        return min(reliable, key=lambda g: g.median_performance) if reliable else None


@dataclass(frozen=True)
class PostFeatures:
    has_media: bool
    has_link: bool
    is_thread: bool
    char_count: int
    post_type: str


def length_bucket(char_count: int) -> str:
    if char_count < 100:
        return "short (<100 chars)"
    if char_count < 200:
        return "medium (100-199)"
    return "long (200+)"


def analyse_formats(posts: list[tuple[PostFeatures, float]]) -> FormatAnalysis:
    """Compare performance across format groups."""
    analysis = FormatAnalysis(total_posts=len(posts))

    if len(posts) < MIN_TOTAL_POSTS:
        analysis.caveats.append(
            f"Only {len(posts)} posts available; at least {MIN_TOTAL_POSTS} are "
            f"needed to compare formats meaningfully."
        )
        return analysis

    groups: dict[str, list[float]] = {}
    for features, performance in posts:
        groups.setdefault("with media" if features.has_media else "without media", []).append(
            performance
        )
        groups.setdefault("with link" if features.has_link else "without link", []).append(
            performance
        )
        groups.setdefault("thread" if features.is_thread else "single post", []).append(performance)
        groups.setdefault(length_bucket(features.char_count), []).append(performance)
        groups.setdefault(features.post_type.lower(), []).append(performance)

    analysis.groups = sorted(
        (_bucket(label, values) for label, values in groups.items()),
        key=lambda b: b.median_performance,
        reverse=True,
    )
    analysis.is_reliable = any(g.is_reliable for g in analysis.groups)

    thin = [g.label for g in analysis.groups if not g.is_reliable]
    if thin:
        analysis.caveats.append(
            f"These groups have fewer than {MIN_POSTS_PER_BUCKET} posts and are not "
            f"conclusions: {', '.join(sorted(thin))}."
        )

    return analysis


def compare_groups(a: Bucket, b: Bucket) -> str:
    """Plain-language comparison of two format groups.

    Refuses to compare when either side is under-sampled, rather than reporting
    a difference that rests on one or two posts.
    """
    if not (a.is_reliable and b.is_reliable):
        return (
            f"Cannot compare {a.label} with {b.label}: "
            f"{a.label} has {a.sample_size} post(s), {b.label} has {b.sample_size}."
        )
    if b.median_performance == 0:
        return f"{b.label} has a median of zero, so no ratio is meaningful."

    ratio = (a.median_performance - b.median_performance) / b.median_performance
    if abs(ratio) < 0.1:
        return f"{a.label} and {b.label} perform about the same."
    better = "better" if ratio > 0 else "worse"
    return f"{a.label} performs {abs(ratio):.0%} {better} than {b.label} (median)."
