"""Analytics engine tests.

The attribution tests matter most. That model produces numbers that look
authoritative, so it is tested two ways: it must recover a known signal from
synthetic data, and — equally important — it must *refuse* or widen its
intervals when the data genuinely cannot support a conclusion. A model that
always answers confidently is worse than useless here.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta

import pytest

from app.analytics import baselines, metrics, patterns
from app.analytics.attribution import (
    AttributionConfidence,
    attribute_followers,
    response_curve,
)
from app.analytics.revenue import (
    RevenueRecord,
    revenue_per_campaign,
    revenue_per_post,
    summarise,
)
from app.models.enums import Provenance
from app.models.revenue import RevenueSourceType

BASE = datetime(2026, 7, 1, tzinfo=UTC)


# --------------------------------------------------------------------- metrics
class TestEngagementRate:
    def test_prefers_impressions(self) -> None:
        rate = metrics.engagement_rate(50, impressions=1000, followers=5000)
        assert rate.denominator is metrics.Denominator.IMPRESSIONS
        assert rate.value == pytest.approx(0.05)

    def test_falls_back_to_followers(self) -> None:
        rate = metrics.engagement_rate(50, impressions=None, followers=5000)
        assert rate.denominator is metrics.Denominator.FOLLOWERS
        assert rate.value == pytest.approx(0.01)

    def test_unavailable_when_no_denominator(self) -> None:
        """Not zero — undefined. The UI must render a gap, not a 0%."""
        rate = metrics.engagement_rate(50)
        assert rate.value is None
        assert rate.provenance is Provenance.UNAVAILABLE
        assert not rate.is_available

    def test_zero_impressions_is_not_a_denominator(self) -> None:
        """A post with no impressions has an undefined rate, not an infinite one."""
        rate = metrics.engagement_rate(5, impressions=0, followers=100)
        assert rate.denominator is metrics.Denominator.FOLLOWERS

    def test_denominator_is_always_reported(self) -> None:
        """Per-impression and per-follower can tell opposite stories."""
        per_impression = metrics.engagement_rate(50, impressions=500)
        per_follower = metrics.engagement_rate(50, followers=50_000)
        assert per_impression.value > per_follower.value
        assert "impression" in per_impression.describe()
        assert "follower" in per_follower.describe()

    def test_weighted_engagement_ranks_effort(self) -> None:
        reposts = metrics.weighted_engagement(retweet_count=10)
        likes = metrics.weighted_engagement(like_count=10)
        assert reposts > likes


class TestVelocity:
    def test_computes_per_hour_rate(self) -> None:
        points = [
            metrics.VelocityPoint(0, 0, None),
            metrics.VelocityPoint(1, 100, None),
            metrics.VelocityPoint(2, 150, None),
        ]
        velocity = metrics.engagement_velocity(points)
        assert velocity[0][1] == pytest.approx(100)
        assert velocity[1][1] == pytest.approx(50)

    def test_negative_revisions_clamp_to_zero(self) -> None:
        """X revises counters down (deleted replies, spam removal)."""
        points = [
            metrics.VelocityPoint(0, 100, None),
            metrics.VelocityPoint(1, 80, None),
        ]
        assert metrics.engagement_velocity(points)[0][1] == 0.0

    def test_peak_velocity(self) -> None:
        points = [
            metrics.VelocityPoint(0, 0, None),
            metrics.VelocityPoint(1, 10, None),
            metrics.VelocityPoint(2, 200, None),
            metrics.VelocityPoint(3, 205, None),
        ]
        peak = metrics.peak_velocity(points)
        assert peak is not None
        assert peak[0] == 2


class TestPercentileAndRpm:
    def test_percentile_rank(self) -> None:
        population = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert metrics.percentile_rank(5.0, population) == 90.0
        assert metrics.percentile_rank(1.0, population) == 10.0

    def test_percentile_of_empty_population_is_none(self) -> None:
        assert metrics.percentile_rank(1.0, []) is None

    def test_rpm_requires_impressions(self) -> None:
        assert metrics.rpm(10_000, None) is None
        assert metrics.rpm(10_000, 0) is None
        assert metrics.rpm(10_000, 50_000) == pytest.approx(200.0)


# ------------------------------------------------------------------ baselines
class TestBaselines:
    def test_requires_minimum_observations(self) -> None:
        baseline = baselines.compute_baseline([1.0, 2.0, 3.0])
        assert not baseline.is_reliable
        assert "at least" in baseline.note

    def test_median_resists_a_single_outlier(self) -> None:
        """The whole reason for median/MAD over mean/sigma."""
        normal = [10.0] * 20
        with_viral = [*normal, 10_000.0]

        baseline_before = baselines.compute_baseline(normal)
        baseline_after = baselines.compute_baseline(with_viral)

        # The mean would have moved by ~475; the median barely moves.
        assert abs(baseline_after.median - baseline_before.median) < 1.0
        assert statistics.mean(with_viral) > 400

    def test_viral_post_is_flagged_not_absorbed(self) -> None:
        history = [10.0, 12.0, 9.0, 11.0, 10.0, 13.0, 8.0, 11.0, 10.0, 12.0]
        baseline = baselines.compute_baseline(history)
        anomaly = baselines.detect_anomaly(500.0, baseline)
        assert anomaly.direction is baselines.AnomalyDirection.SPIKE
        assert anomaly.severity is baselines.AnomalySeverity.EXTREME

    def test_drop_is_detected(self) -> None:
        history = [100.0, 105.0, 98.0, 102.0, 100.0, 103.0, 99.0, 101.0]
        baseline = baselines.compute_baseline(history)
        anomaly = baselines.detect_anomaly(10.0, baseline)
        assert anomaly.direction is baselines.AnomalyDirection.DROP

    def test_normal_value_is_not_flagged(self) -> None:
        history = [10.0, 12.0, 9.0, 11.0, 10.0, 13.0, 8.0, 11.0]
        baseline = baselines.compute_baseline(history)
        assert not baselines.detect_anomaly(11.0, baseline).is_anomalous

    def test_unreliable_baseline_never_flags(self) -> None:
        """Too little data must produce silence, not false alarms."""
        baseline = baselines.compute_baseline([5.0, 6.0])
        assert not baselines.detect_anomaly(1000.0, baseline).is_anomalous

    def test_flat_history_reports_without_inventing_a_z_score(self) -> None:
        baseline = baselines.compute_baseline([10.0] * 10)
        anomaly = baselines.detect_anomaly(50.0, baseline)
        assert anomaly.z_score is None
        assert anomaly.is_anomalous


class TestSeasonality:
    def test_requires_enough_per_weekday(self) -> None:
        """One Tuesday must not define 'typical Tuesday'."""
        observations = [(BASE + timedelta(days=i * 7), 10.0) for i in range(2)]
        assert not baselines.compute_seasonality(observations).is_reliable

    def test_builds_profile_with_enough_data(self) -> None:
        observations = [(BASE + timedelta(days=i), float(i % 7)) for i in range(56)]
        profile = baselines.compute_seasonality(observations)
        assert profile.is_reliable
        assert len(profile.by_weekday) == 7


class TestTrend:
    def test_detects_rise_and_fall(self) -> None:
        rising = baselines.compare_periods([20.0] * 5, [10.0] * 5)
        assert rising.direction == "rising"
        falling = baselines.compare_periods([5.0] * 5, [10.0] * 5)
        assert falling.direction == "falling"

    def test_insufficient_data_is_not_a_trend(self) -> None:
        assert not baselines.compare_periods([1.0], [2.0]).is_reliable

    def test_uses_medians_so_one_spike_does_not_flip_it(self) -> None:
        """A declining account with one viral post is still declining."""
        recent = [2.0, 2.0, 2.0, 2.0, 900.0]
        prior = [10.0, 10.0, 10.0, 10.0, 10.0]
        assert baselines.compare_periods(recent, prior).direction == "falling"


# ---------------------------------------------------------------- attribution
def hourly_followers(
    hours: int, *, base_rate: float, bumps: list[tuple[int, float]] | None = None
) -> list[tuple[datetime, int]]:
    """Synthesise a follower series with known post-driven bumps."""
    snapshots: list[tuple[datetime, int]] = []
    count = 10_000.0
    for hour in range(hours):
        count += base_rate
        for bump_hour, magnitude in bumps or []:
            if hour >= bump_hour:
                # Same exponential shape the model assumes.
                import math

                count += magnitude * math.exp(-(hour - bump_hour) / 12.0) / 12.0
        snapshots.append((BASE + timedelta(hours=hour), int(count)))
    return snapshots


class TestAttributionRefusal:
    def test_refuses_with_too_little_history(self) -> None:
        """Three days is the floor; below it this is noise-fitting."""
        snapshots = hourly_followers(40, base_rate=1.0)
        posts = [("a", BASE + timedelta(hours=5)), ("b", BASE + timedelta(hours=20))]
        report = attribute_followers(snapshots, posts)

        assert not report.is_usable
        assert report.confidence is AttributionConfidence.NONE
        assert "noise-fitting" in report.caveats[0]

    def test_refuses_with_too_few_posts(self) -> None:
        snapshots = hourly_followers(200, base_rate=1.0)
        report = attribute_followers(snapshots, [("only", BASE + timedelta(hours=10))])
        assert not report.is_usable

    def test_always_marked_inferred(self) -> None:
        """Non-negotiable: this is modelled, never measured."""
        report = attribute_followers(hourly_followers(20, base_rate=1.0), [])
        assert report.provenance is Provenance.INFERRED

    def test_caveat_states_the_limitation_plainly(self) -> None:
        snapshots = hourly_followers(400, base_rate=2.0, bumps=[(50, 500.0)])
        posts = [("a", BASE + timedelta(hours=50)), ("b", BASE + timedelta(hours=200))]
        report = attribute_followers(snapshots, posts)
        assert any("modelled, not measured" in c for c in report.caveats)


class TestAttributionRecovery:
    def test_recovers_a_known_single_driver(self) -> None:
        """One post causes a bump; the model must find it."""
        snapshots = hourly_followers(500, base_rate=1.0, bumps=[(100, 600.0)])
        posts = [
            ("driver", BASE + timedelta(hours=100)),
            ("quiet", BASE + timedelta(hours=300)),
        ]
        report = attribute_followers(snapshots, posts)

        assert report.is_usable
        top = report.contributions[0]
        assert top.post_id == "driver"
        assert top.estimate > report.contributions[1].estimate

    def test_ranks_two_drivers_correctly(self) -> None:
        snapshots = hourly_followers(600, base_rate=1.0, bumps=[(100, 800.0), (350, 200.0)])
        posts = [
            ("big", BASE + timedelta(hours=100)),
            ("small", BASE + timedelta(hours=350)),
        ]
        report = attribute_followers(snapshots, posts)

        by_id = {c.post_id: c for c in report.contributions}
        assert by_id["big"].estimate > by_id["small"].estimate

    def test_quiet_post_is_not_credited(self) -> None:
        """A post followed by no growth must not be handed a share."""
        snapshots = hourly_followers(500, base_rate=1.0, bumps=[(100, 600.0)])
        posts = [
            ("driver", BASE + timedelta(hours=100)),
            ("quiet", BASE + timedelta(hours=300)),
        ]
        report = attribute_followers(snapshots, posts)
        quiet = next(c for c in report.contributions if c.post_id == "quiet")
        assert not quiet.is_distinguishable


class TestAttributionHonesty:
    def test_simultaneous_posts_are_reported_as_inseparable(self) -> None:
        """Two posts an hour apart cannot be told apart — say so."""
        snapshots = hourly_followers(500, base_rate=1.0, bumps=[(100, 600.0)])
        posts = [
            ("first", BASE + timedelta(hours=100)),
            ("second", BASE + timedelta(hours=101)),
        ]
        report = attribute_followers(snapshots, posts)
        assert any("within three hours" in c for c in report.caveats)

    def test_contributions_are_never_negative(self) -> None:
        """A post cannot cause negative followers; the constraint enforces it."""
        snapshots = hourly_followers(500, base_rate=5.0, bumps=[(100, 400.0)])
        posts = [
            ("a", BASE + timedelta(hours=100)),
            ("b", BASE + timedelta(hours=250)),
            ("c", BASE + timedelta(hours=400)),
        ]
        report = attribute_followers(snapshots, posts)
        assert all(c.estimate >= 0 for c in report.contributions)
        assert all(c.ci_low >= 0 for c in report.contributions)

    def test_confidence_intervals_contain_the_estimate(self) -> None:
        """Regression: the point estimate must lie inside its own interval.

        An earlier version took raw bootstrap percentiles. Because the estimator
        is ridge-penalised, every bootstrap refit shrank a second time and the
        percentiles came out biased low — far enough that estimates fell outside
        their intervals entirely. An earlier version of this test only checked
        ci_low <= ci_high, which that bug passed.
        """
        snapshots = hourly_followers(600, base_rate=1.0, bumps=[(100, 800.0), (350, 200.0)])
        posts = [
            ("big", BASE + timedelta(hours=100)),
            ("small", BASE + timedelta(hours=350)),
            ("none", BASE + timedelta(hours=500)),
        ]
        report = attribute_followers(snapshots, posts)
        assert report.contributions
        for c in report.contributions:
            assert c.ci_low <= c.estimate <= c.ci_high, (
                f"{c.post_id}: estimate {c.estimate} outside [{c.ci_low}, {c.ci_high}]"
            )

    def test_recovers_known_magnitudes_not_just_ranking(self) -> None:
        """The estimates must be roughly right, not merely correctly ordered."""
        snapshots = hourly_followers(600, base_rate=1.0, bumps=[(100, 800.0), (350, 200.0)])
        posts = [
            ("big", BASE + timedelta(hours=100)),
            ("small", BASE + timedelta(hours=350)),
        ]
        report = attribute_followers(snapshots, posts)
        by_id = {c.post_id: c for c in report.contributions}
        assert by_id["big"].estimate == pytest.approx(800, rel=0.25)
        assert by_id["small"].estimate == pytest.approx(200, rel=0.35)

    def test_unexplained_growth_is_reported(self) -> None:
        """Followers arrive for reasons other than posting; do not invent causes."""
        snapshots = hourly_followers(500, base_rate=5.0)
        posts = [("a", BASE + timedelta(hours=100)), ("b", BASE + timedelta(hours=300))]
        report = attribute_followers(snapshots, posts)
        assert report.total_observed_growth > 0

    def test_collection_gaps_are_dropped_not_interpolated(self) -> None:
        """A gap means collection was down — inventing growth across it would lie."""
        snapshots = hourly_followers(200, base_rate=1.0)
        gapped = snapshots[:50] + [(t + timedelta(hours=48), c + 500) for t, c in snapshots[50:]]
        report = attribute_followers(
            gapped, [("a", BASE + timedelta(hours=10)), ("b", BASE + timedelta(hours=100))]
        )
        # The 48h jump must not be treated as one enormous hourly delta.
        assert report.hours_of_history < len(gapped)


class TestResponseCurve:
    def test_zero_before_publication(self) -> None:
        import numpy as np

        curve = response_curve(np.array([-5.0, -1.0, 0.0, 1.0]), 12.0)
        assert curve[0] == 0.0
        assert curve[1] == 0.0
        assert curve[3] > 0

    def test_decays_monotonically(self) -> None:
        import numpy as np

        curve = response_curve(np.array([0.0, 6.0, 12.0, 24.0, 48.0]), 12.0)
        assert list(curve) == sorted(curve, reverse=True)

    def test_normalised(self) -> None:
        import numpy as np

        curve = response_curve(np.arange(0.0, 72.0), 12.0)
        assert curve.sum() == pytest.approx(1.0)


# ------------------------------------------------------------------- patterns
class TestTimingAnalysis:
    def test_withholds_below_minimum_posts(self) -> None:
        posts = [(BASE + timedelta(days=i), 0.05) for i in range(5)]
        analysis = patterns.analyse_timing(posts)
        assert not analysis.is_reliable
        assert "At least" in analysis.caveats[0]

    def test_never_recommends_an_hour_with_too_few_posts(self) -> None:
        """The 'post at 3am Tuesday' failure mode, prevented explicitly."""
        posts = [(BASE.replace(hour=9) + timedelta(days=i), 0.02) for i in range(20)]
        # One spectacular 3am post.
        posts.append((BASE.replace(hour=3) + timedelta(days=30), 0.99))

        analysis = patterns.analyse_timing(posts)
        assert "03:00" not in [b.label for b in analysis.best_hours]

    def test_identifies_a_genuinely_better_hour(self) -> None:
        posts = [(BASE.replace(hour=9) + timedelta(days=i), 0.10) for i in range(10)]
        posts += [(BASE.replace(hour=18) + timedelta(days=i), 0.02) for i in range(10)]
        analysis = patterns.analyse_timing(posts)
        assert analysis.is_reliable
        assert analysis.best_hours[0].label == "09:00"

    def test_two_hour_comparison_is_caveated_as_narrow(self) -> None:
        """Comparing two hours is valid, but it is not 'the best time to post'."""
        posts = [(BASE.replace(hour=9) + timedelta(days=i), 0.10) for i in range(10)]
        posts += [(BASE.replace(hour=18) + timedelta(days=i), 0.02) for i in range(10)]
        analysis = patterns.analyse_timing(posts)
        assert any("not that either is the best available" in c for c in analysis.caveats)

    def test_uses_the_owners_timezone(self) -> None:
        posts = [(BASE.replace(hour=12) + timedelta(days=i), 0.05) for i in range(20)]
        utc = patterns.analyse_timing(posts, "UTC")
        lagos = patterns.analyse_timing(posts, "Africa/Lagos")
        assert utc.by_hour[0].label != lagos.by_hour[0].label

    def test_unknown_timezone_falls_back_to_utc(self) -> None:
        posts = [(BASE + timedelta(days=i), 0.05) for i in range(20)]
        assert patterns.analyse_timing(posts, "Mars/Olympus").timezone == "UTC"


class TestFormatAnalysis:
    def _features(self, **kwargs: object) -> patterns.PostFeatures:
        defaults = {
            "has_media": False,
            "has_link": False,
            "is_thread": False,
            "char_count": 150,
            "post_type": "ORIGINAL",
        }
        return patterns.PostFeatures(**{**defaults, **kwargs})  # type: ignore[arg-type]

    def test_withholds_below_minimum(self) -> None:
        posts = [(self._features(), 0.05) for _ in range(5)]
        assert not patterns.analyse_formats(posts).is_reliable

    def test_identifies_the_better_format(self) -> None:
        posts = [(self._features(has_media=True), 0.10) for _ in range(10)]
        posts += [(self._features(has_media=False), 0.02) for _ in range(10)]
        analysis = patterns.analyse_formats(posts)
        assert analysis.best is not None
        assert analysis.best.label == "with media"

    def test_refuses_to_compare_undersampled_groups(self) -> None:
        big = patterns.Bucket("with media", 20, 0.1, True)
        tiny = patterns.Bucket("thread", 1, 0.9, False)
        assert "Cannot compare" in patterns.compare_groups(tiny, big)

    def test_length_buckets(self) -> None:
        assert "short" in patterns.length_bucket(50)
        assert "medium" in patterns.length_bucket(150)
        assert "long" in patterns.length_bucket(250)


# -------------------------------------------------------------------- revenue
def record(amount: int, day: int = 1, month: int = 7, **kwargs: object) -> RevenueRecord:
    defaults = {
        "amount_minor": amount,
        "currency": "USD",
        "earned_at": datetime(2026, month, day).date(),
        "source_type": RevenueSourceType.X_ADS_SHARE,
    }
    return RevenueRecord(**{**defaults, **kwargs})  # type: ignore[arg-type]


class TestRevenueSummary:
    def test_totals_and_source_breakdown(self) -> None:
        records = [
            record(10_000),
            record(5_000, source_type=RevenueSourceType.SPONSORSHIP),
            record(2_500, source_type=RevenueSourceType.AFFILIATE),
        ]
        summary = summarise(records)
        assert summary.total_minor == 17_500
        assert summary.best_source is not None
        assert summary.best_source.source_type is RevenueSourceType.X_ADS_SHARE

    def test_excludes_other_currencies_rather_than_summing_them(self) -> None:
        """Adding NGN to USD without a rate would be arithmetic on nonsense."""
        records = [record(10_000), record(50_000, currency="NGN")]
        summary = summarise(records, "USD")
        assert summary.total_minor == 10_000
        assert any("other currencies" in c for c in summary.caveats)

    def test_month_over_month_growth(self) -> None:
        records = [record(10_000, month=6), record(15_000, month=7)]
        summary = summarise(records)
        assert summary.growth_ratio == pytest.approx(0.5)

    def test_zero_previous_month_yields_no_ratio(self) -> None:
        summary = summarise([record(0, month=6), record(15_000, month=7)])
        assert summary.growth_ratio is None

    def test_provenance_is_user_entered(self) -> None:
        """X has no earnings API; this can never be MEASURED."""
        assert summarise([record(100)]).provenance is Provenance.USER_ENTERED

    def test_money_stays_integral(self) -> None:
        records = [record(333) for _ in range(3)]
        assert summarise(records).total_minor == 999


class TestRevenuePerPost:
    def test_computes_rpm_where_impressions_exist(self) -> None:
        records = [record(10_000, post_id="p1")]
        results = revenue_per_post(records, {"p1": 50_000})
        assert results[0].rpm_available
        assert results[0].rpm_minor == pytest.approx(200.0)

    def test_suppresses_rpm_when_impressions_were_never_collected(self) -> None:
        """The whole point: a confidently wrong RPM invites bad decisions."""
        records = [record(10_000, post_id="p1")]
        results = revenue_per_post(records, {"p1": None})
        assert not results[0].rpm_available
        assert results[0].rpm_minor is None
        assert "30-day window" in (results[0].reason_unavailable or "")

    def test_zero_impressions_is_undefined_not_infinite(self) -> None:
        results = revenue_per_post([record(10_000, post_id="p1")], {"p1": 0})
        assert not results[0].rpm_available
        assert results[0].rpm_minor is None

    def test_unattributed_revenue_is_not_forced_onto_posts(self) -> None:
        assert revenue_per_post([record(10_000)], {}) == []


class TestRevenuePerCampaign:
    def test_fulfilment_ratio(self) -> None:
        records = [record(7_500, campaign_id="c1")]
        results = revenue_per_campaign(records, {"c1": 10_000})
        assert results[0].fulfilment_ratio == pytest.approx(0.75)

    def test_no_contract_means_no_ratio(self) -> None:
        results = revenue_per_campaign([record(7_500, campaign_id="c1")], {"c1": None})
        assert results[0].fulfilment_ratio is None
