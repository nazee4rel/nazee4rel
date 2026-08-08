"""Snapshot scheduling tests.

This module decides what gets collected and what does not, so a bug here shows
up as missing data rather than as an error — and past 30 days, missing data
cannot be recovered. Hence the exhaustive coverage, especially of the freeze
path and its interaction with the budget governor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.collectors import schedule
from app.collectors.schedule import BudgetState, SnapshotPriority

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def posted_hours_ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def decide(
    age_hours: float,
    last_snapshot_hours_ago: float | None = None,
    *,
    frozen: bool = False,
    impressions_collected: bool = False,
    budget: BudgetState = BudgetState.HEALTHY,
) -> schedule.SnapshotDecision:
    return schedule.decide(
        posted_at=posted_hours_ago(age_hours),
        last_snapshot_at=(
            None if last_snapshot_hours_ago is None else posted_hours_ago(last_snapshot_hours_ago)
        ),
        has_final_freeze=frozen,
        impressions_ever_collected=impressions_collected,
        now=NOW,
        budget_state=budget,
    )


class TestCadenceLadder:
    def test_hourly_in_the_first_day(self) -> None:
        assert decide(6, last_snapshot_hours_ago=1.5).due
        assert not decide(6, last_snapshot_hours_ago=0.5).due

    def test_six_hourly_in_the_first_week(self) -> None:
        assert decide(72, last_snapshot_hours_ago=7).due
        assert not decide(72, last_snapshot_hours_ago=3).due

    def test_daily_after_the_first_week(self) -> None:
        assert decide(24 * 14, last_snapshot_hours_ago=25).due
        assert not decide(24 * 14, last_snapshot_hours_ago=10).due

    def test_never_snapshotted_is_always_due(self) -> None:
        """A post discovered late must not be left with no record at all."""
        assert decide(100, last_snapshot_hours_ago=None).due

    @pytest.mark.parametrize(
        ("hours", "expected"),
        [
            (1, "first_day"),
            (23.9, "first_day"),
            (25, "first_week"),
            (24 * 6, "first_week"),
            (24 * 10, "first_month"),
            (24 * 28, "first_month"),
        ],
    )
    def test_tier_boundaries(self, hours: float, expected: str) -> None:
        tier = schedule.tier_for_age(hours)
        assert tier is not None
        assert tier.name == expected

    def test_fresh_posts_outrank_mature_ones(self) -> None:
        """Velocity is front-loaded, so recent posts matter more per snapshot."""
        assert decide(2, 2).priority < decide(24 * 20, 30).priority


class TestFinalFreeze:
    def test_freeze_is_due_inside_the_window(self) -> None:
        decision = decide(24 * 29.5, last_snapshot_hours_ago=1)
        assert decision.due
        assert decision.is_final_freeze
        assert decision.priority is SnapshotPriority.FINAL_FREEZE

    def test_freeze_outranks_everything(self) -> None:
        assert SnapshotPriority.FINAL_FREEZE < SnapshotPriority.NEVER_CAPTURED
        assert SnapshotPriority.FINAL_FREEZE < SnapshotPriority.FRESH

    def test_freeze_survives_a_critical_budget(self) -> None:
        """The governor may reduce resolution; it may not forfeit the freeze."""
        decision = decide(24 * 29.5, last_snapshot_hours_ago=0.1, budget=BudgetState.CRITICAL)
        assert decision.due
        assert decision.is_final_freeze

    def test_freeze_survives_an_exhausted_budget(self) -> None:
        """The one thing that still runs at zero budget.

        Skipping an ordinary snapshot costs resolution. Skipping this costs the
        impressions permanently, so it is the last thing surrendered.
        """
        decision = decide(24 * 29.5, last_snapshot_hours_ago=0.1, budget=BudgetState.EXHAUSTED)
        assert decision.due
        assert decision.is_final_freeze

    def test_ordinary_snapshots_stop_when_budget_exhausted(self) -> None:
        decision = decide(5, last_snapshot_hours_ago=10, budget=BudgetState.EXHAUSTED)
        assert not decision.due
        assert "Budget exhausted" in decision.reason

    def test_freeze_not_repeated_once_taken(self) -> None:
        assert not decide(24 * 29.5, last_snapshot_hours_ago=0.1, frozen=True).due

    def test_freeze_window_opens_a_day_early(self) -> None:
        """Slack before the cliff leaves room for retries after a failure."""
        assert not schedule.is_in_freeze_window(24 * 28.5)
        assert schedule.is_in_freeze_window(24 * 29.1)
        assert schedule.is_in_freeze_window(24 * 29.99)

    def test_freeze_window_closes_at_the_cliff(self) -> None:
        assert not schedule.is_in_freeze_window(24 * 30.1)


class TestPastTheCliff:
    def test_no_snapshots_after_thirty_days(self) -> None:
        decision = decide(24 * 31, last_snapshot_hours_ago=100)
        assert not decision.due
        assert "30-day window" in decision.reason

    def test_cliff_applies_even_without_a_freeze(self) -> None:
        """Past the cliff the data is gone; there is nothing left to rescue."""
        assert not decide(24 * 35, last_snapshot_hours_ago=None, frozen=False).due

    def test_window_close_time(self) -> None:
        posted = datetime(2026, 8, 1, 9, 30, tzinfo=UTC)
        assert schedule.window_closes_at(posted) == datetime(2026, 8, 31, 9, 30, tzinfo=UTC)

    def test_naive_timestamps_are_treated_as_utc(self) -> None:
        naive = datetime(2026, 8, 1, 9, 30)
        assert schedule.window_closes_at(naive).tzinfo is not None


class TestBudgetDegradation:
    def test_warning_halves_the_sampling_rate(self) -> None:
        tier = schedule.CADENCE_TIERS[0]  # hourly
        assert schedule.effective_interval(tier, BudgetState.HEALTHY) == 1.0
        assert schedule.effective_interval(tier, BudgetState.WARNING) == 2.0

    def test_critical_forces_daily(self) -> None:
        for tier in schedule.CADENCE_TIERS:
            assert schedule.effective_interval(tier, BudgetState.CRITICAL) == 24.0

    def test_warning_never_stretches_beyond_daily(self) -> None:
        daily_tier = schedule.CADENCE_TIERS[-1]
        assert schedule.effective_interval(daily_tier, BudgetState.WARNING) == 24.0

    def test_degradation_changes_what_is_due(self) -> None:
        """A post due hourly is not due under a warning budget."""
        assert decide(5, last_snapshot_hours_ago=1.5, budget=BudgetState.HEALTHY).due
        assert not decide(5, last_snapshot_hours_ago=1.5, budget=BudgetState.WARNING).due


class TestCostEstimation:
    def test_snapshots_per_post_is_plausible(self) -> None:
        """Matches the ~71 figure the architecture doc costed the design on."""
        count = schedule.expected_snapshots_per_post()
        assert 60 <= count <= 85

    def test_monthly_estimate_scales_with_posting_rate(self) -> None:
        five = schedule.estimate_monthly_resources(5)
        ten = schedule.estimate_monthly_resources(10)
        assert ten == pytest.approx(five * 2, rel=0.05)

    def test_five_posts_a_day_matches_the_documented_cost(self) -> None:
        """~10,650 reads/month ≈ $10.65 at the Owned Reads rate."""
        resources = schedule.estimate_monthly_resources(5)
        assert 9_000 <= resources <= 12_500
        assert 9.0 <= resources * 0.001 <= 12.5

    def test_silent_account_costs_nothing(self) -> None:
        assert schedule.estimate_monthly_resources(0) == 0


class TestPrioritySorting:
    def test_freeze_sorts_first(self) -> None:
        items: list[tuple[object, schedule.SnapshotDecision]] = [
            ("mature", schedule.SnapshotDecision(True, "", SnapshotPriority.MATURE)),
            ("freeze", schedule.SnapshotDecision(True, "", SnapshotPriority.FINAL_FREEZE, True)),
            ("fresh", schedule.SnapshotDecision(True, "", SnapshotPriority.FRESH)),
        ]
        assert [key for key, _ in schedule.sort_by_priority(items)] == [
            "freeze",
            "fresh",
            "mature",
        ]

    def test_undue_items_are_dropped(self) -> None:
        items: list[tuple[object, schedule.SnapshotDecision]] = [
            ("due", schedule.SnapshotDecision(True, "")),
            ("not-due", schedule.SnapshotDecision(False, "")),
        ]
        assert [key for key, _ in schedule.sort_by_priority(items)] == ["due"]


class TestAgeHelpers:
    def test_age_is_never_negative(self) -> None:
        """Clock skew must not produce a negative age and corrupt tier choice."""
        future = NOW + timedelta(hours=5)
        assert schedule.age_hours(future, NOW) == 0.0

    def test_decision_is_truthy(self) -> None:
        assert bool(schedule.SnapshotDecision(True, "")) is True
        assert bool(schedule.SnapshotDecision(False, "")) is False
