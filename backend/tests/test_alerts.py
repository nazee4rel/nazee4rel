"""Alerting tests.

Most of these assert that something did *not* fire. That is the right emphasis:
detection is arithmetic the analytics engine already does, whereas an alerting
system's actual failure mode is firing so often that the channel gets muted —
after which the one alert that mattered goes unread too.

So the cases below are dominated by thin baselines, duplicate events, cooldowns
and conditions that resolved themselves.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts import rules
from app.alerts.channels import EmailChannel, Message, build_channels, redact_address
from app.alerts.rules import AlertContext
from app.alerts.service import AlertService
from app.models.alerting import (
    Alert,
    AlertRuleKey,
    AlertSeverity,
    AlertState,
    Delivery,
    DeliveryChannel,
    DeliveryStatus,
    Report,
    ReportPeriod,
)
from app.models.content import AccountMetricSnapshot, Post, PostMetricSnapshot
from app.models.enums import Provenance
from app.models.revenue import RevenueEntry, RevenueSourceType
from app.models.user import User
from app.models.x_account import XAccount
from app.reports.service import ReportService, render_text
from app.services.analytics_service import DashboardSummary, FollowerSeries

NOW = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------ fixtures


async def make_account(db: AsyncSession) -> XAccount:
    user = User(
        email=f"al{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
        display_name="Alerts",
        timezone="UTC",
    )
    db.add(user)
    await db.flush()
    account = XAccount(
        user_id=user.id,
        x_user_id=uuid.uuid4().hex[:12],
        username="alerted",
        connected_at=NOW,
    )
    db.add(account)
    await db.flush()
    return account


async def seed(
    db: AsyncSession,
    account: XAccount,
    *,
    days: int = 21,
    per_day_gain: int = 10,
    final_day_gain: int | None = None,
    posts_per_day: int = 1,
    likes: int = 20,
    impressions: int | None = 1000,
) -> None:
    """A steady account, optionally with one unusual final day.

    Snapshots are hourly, matching the real collector: at any coarser interval
    the series would look like one long collection gap, and the fixture would be
    testing something the system never sees. Daily gains carry a little jitter
    so the baseline has a spread — a perfectly flat history is an edge case
    worth testing deliberately, not one to stumble into.
    """
    start = NOW.replace(hour=0) - timedelta(days=days)
    jitter = [0, 2, -1, 1, -2, 3, -3]
    followers = 5000.0
    for day in range(days + 1):
        gain = per_day_gain + jitter[day % len(jitter)]
        if final_day_gain is not None and day == days:
            gain = final_day_gain
        opening = followers
        for hour in range(24):
            captured = start + timedelta(days=day, hours=hour)
            if captured > NOW:
                break
            db.add(
                AccountMetricSnapshot(
                    x_account_id=account.id,
                    captured_at=captured,
                    # Spread the day's change across its hours, so the daily
                    # maximum lands on the last hour like a real series.
                    followers_count=int(opening + gain * (hour + 1) / 24),
                    provenance=Provenance.MEASURED,
                )
            )
        followers = opening + gain

    for day in range(days):
        for index in range(posts_per_day):
            posted_at = start + timedelta(days=day, hours=9 + index * 4)
            post = Post(
                x_account_id=account.id,
                x_post_id=uuid.uuid4().hex[:12],
                text=f"post {day}-{index}",
                posted_at=posted_at,
                first_seen_at=posted_at,
                metrics_window_closes_at=posted_at + timedelta(days=30),
                char_count=40,
            )
            db.add(post)
            await db.flush()
            db.add(
                PostMetricSnapshot(
                    post_id=post.id,
                    captured_at=posted_at + timedelta(hours=24),
                    post_age_hours=24.0,
                    like_count=likes,
                    impression_count=impressions,
                    non_public_available=impressions is not None,
                    provenance=Provenance.MEASURED,
                )
            )
    await db.flush()


async def context_for(db: AsyncSession, account: XAccount, **overrides: object) -> AlertContext:
    ctx = await AlertService(db).build_context(account)
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


# --------------------------------------------------------------- registry


class TestRegistry:
    def test_every_rule_key_has_a_spec(self) -> None:
        """A key with no spec would be an alert nobody can configure or explain."""
        assert set(rules.REGISTRY) == set(AlertRuleKey)

    def test_stateful_rules_are_the_operational_ones(self) -> None:
        """Only conditions that can end are allowed to resolve themselves."""
        assert rules.STATEFUL_RULES == {
            AlertRuleKey.COLLECTION_STALLED,
            AlertRuleKey.FREEZE_AT_RISK,
            AlertRuleKey.BUDGET_PRESSURE,
            AlertRuleKey.ACCESS_DEGRADED,
        }

    def test_every_rule_describes_itself(self) -> None:
        for spec in rules.REGISTRY.values():
            assert spec.description.strip()
            assert spec.default_cooldown_hours > 0


# ---------------------------------------------------------------- detectors


class TestDetectors:
    async def test_thin_history_never_fires_a_spike(self, db_session: AsyncSession) -> None:
        """The central anti-fatigue rule: four days of history is not a baseline."""
        account = await make_account(db_session)
        await seed(db_session, account, days=4, final_day_gain=500)

        ctx = await context_for(db_session, account)
        assert rules.detect_follower_spike(ctx) is None
        assert rules.detect_follower_drop(ctx) is None

    async def test_genuine_spike_fires_once_there_is_a_baseline(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        await seed(db_session, account, days=21, per_day_gain=10, final_day_gain=400)

        finding = rules.detect_follower_spike(await context_for(db_session, account))

        assert finding is not None
        assert finding.key is AlertRuleKey.FOLLOWER_SPIKE
        # The final day is partial (the suite runs mid-day), so the observed
        # change is a fraction of the configured gain — what matters is that it
        # is far outside the usual spread, not that it hits a round number.
        assert finding.facts["daily_change"] > finding.facts["usual_daily_change"] * 3
        # Dated, so tomorrow's spike is a different event.
        assert finding.dedupe_key.startswith("FOLLOWER_SPIKE:")

    async def test_breakout_needs_more_than_being_the_best_post(
        self, db_session: AsyncSession
    ) -> None:
        """In any set of posts one is the best. That is not a breakout."""
        account = await make_account(db_session)
        await seed(db_session, account, days=21, likes=20, impressions=1000)

        assert rules.detect_breakout_post(await context_for(db_session, account)) is None

    async def test_breakout_fires_on_a_genuine_outlier(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        await seed(db_session, account, days=21, likes=20, impressions=1000)

        posted_at = NOW - timedelta(hours=6)
        post = Post(
            x_account_id=account.id,
            x_post_id="breakout",
            text="the one that landed",
            posted_at=posted_at,
            first_seen_at=posted_at,
            metrics_window_closes_at=posted_at + timedelta(days=30),
            char_count=40,
        )
        db_session.add(post)
        await db_session.flush()
        db_session.add(
            PostMetricSnapshot(
                post_id=post.id,
                captured_at=posted_at + timedelta(hours=2),
                post_age_hours=2.0,
                like_count=900,
                impression_count=1000,
                non_public_available=True,
                provenance=Provenance.MEASURED,
            )
        )
        await db_session.flush()

        finding = rules.detect_breakout_post(await context_for(db_session, account))

        assert finding is not None
        assert finding.facts["ratio_to_median"] >= rules.BREAKOUT_MIN_RATIO
        # Per post, so a post that keeps climbing does not re-alert daily.
        assert finding.dedupe_key == f"BREAKOUT_POST:{post.id}"

    async def test_collection_stall_escalates_with_time(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        await seed(db_session, account, days=10)

        quiet = rules.detect_collection_stalled(
            await context_for(db_session, account, hours_since_snapshot=1.0)
        )
        warning = rules.detect_collection_stalled(
            await context_for(db_session, account, hours_since_snapshot=8.0)
        )
        critical = rules.detect_collection_stalled(
            await context_for(db_session, account, hours_since_snapshot=48.0)
        )

        assert quiet is None
        assert warning is not None and warning.severity is AlertSeverity.WARNING
        assert critical is not None and critical.severity is AlertSeverity.CRITICAL
        # No date in the key: one ongoing condition, not a daily event.
        assert critical.dedupe_key == "COLLECTION_STALLED"

    async def test_a_fresh_install_is_not_alerted_for_never_collecting(
        self, db_session: AsyncSession
    ) -> None:
        """Otherwise the very first thing a new user sees is a critical alert."""
        account = await make_account(db_session)
        ctx = await context_for(db_session, account, hours_since_snapshot=None)
        assert rules.detect_collection_stalled(ctx) is None

    async def test_revenue_alert_is_never_measured(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        await seed(db_session, account, days=10)

        finding = rules.detect_revenue_change(
            await context_for(
                db_session,
                account,
                revenue_growth_ratio=0.8,
                revenue_latest_month="2026-08",
            )
        )

        assert finding is not None
        assert finding.provenance is Provenance.USER_ENTERED
        assert "no creator-earnings API" in finding.body

    async def test_churn_alert_states_what_it_cannot_tell_you(
        self, db_session: AsyncSession
    ) -> None:
        """The closest thing to 'suspicious activity' the data honestly supports."""
        account = await make_account(db_session)
        await seed(db_session, account, days=21, per_day_gain=10, final_day_gain=-400)

        finding = rules.detect_unusual_churn(await context_for(db_session, account))

        assert finding is not None
        assert finding.facts["net_change"] < 0
        assert "does not tell you is why" in finding.body

    @pytest.mark.parametrize(
        ("value", "usual", "rising", "must_contain", "must_not_contain"),
        [
            (400.0, 38.0, True, "Unusual follower growth", "fell"),
            # Regression: growth of +5 against a usual +38 is a fall in the
            # growth rate while still being a gain. "Growth fell sharply: +5"
            # is technically true and reads as a bug.
            (5.0, 38.0, False, "slowed to +5", "fell by"),
            (-120.0, 38.0, False, "fell by 120", "+"),
        ],
    )
    def test_headline_wording_agrees_with_the_number(
        self,
        value: float,
        usual: float,
        rising: bool,
        must_contain: str,
        must_not_contain: str,
    ) -> None:
        headline = rules._change_headline(value, usual, rising=rising)
        assert must_contain in headline
        assert must_not_contain not in headline

    async def test_a_perfectly_flat_history_still_reports_a_loss(
        self, db_session: AsyncSession
    ) -> None:
        """The edge case a hand-rolled z-score would have swallowed.

        With zero spread there is no sigma to divide by. Declining to fire would
        mean the most regular accounts — the ones where a sudden loss is most
        obviously wrong — get no alert at all. The finding is raised without a
        sigma figure and says why.
        """
        account = await make_account(db_session)
        start = NOW.replace(hour=0) - timedelta(days=21)
        followers = 5000
        for day in range(22):
            gain = -400 if day == 21 else 10
            opening = followers
            for hour in range(24):
                captured = start + timedelta(days=day, hours=hour)
                if captured > NOW:
                    break
                db_session.add(
                    AccountMetricSnapshot(
                        x_account_id=account.id,
                        captured_at=captured,
                        followers_count=int(opening + gain * (hour + 1) / 24),
                        provenance=Provenance.MEASURED,
                    )
                )
            followers = opening + gain
        await db_session.flush()

        finding = rules.detect_unusual_churn(await context_for(db_session, account))

        assert finding is not None
        assert finding.facts["robust_sigma"] is None
        assert "no spread to measure" in finding.body

    async def test_access_degraded_covers_tokens_and_capabilities(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        await seed(db_session, account, days=10)

        none_yet = rules.detect_access_degraded(await context_for(db_session, account))
        failing = rules.detect_access_degraded(
            await context_for(db_session, account, refresh_failure_count=4)
        )

        assert none_yet is None
        assert failing is not None and failing.severity is AlertSeverity.CRITICAL


# ------------------------------------------------------------------ service


class TestAlertService:
    async def test_rules_are_created_lazily_with_defaults(self, db_session: AsyncSession) -> None:
        """A new rule in the registry should start watching without a migration."""
        account = await make_account(db_session)
        configured = await AlertService(db_session).rules_for(account.id)

        assert set(configured) == set(AlertRuleKey)
        assert all(row.enabled for row in configured.values())
        assert configured[AlertRuleKey.COLLECTION_STALLED].cooldown_hours == 6

    async def test_the_same_event_raises_one_alert(self, db_session: AsyncSession) -> None:
        """Eight evaluations of one viral day must not produce eight alerts."""
        account = await make_account(db_session)
        await seed(db_session, account, days=21, per_day_gain=10, final_day_gain=400)
        service = AlertService(db_session)

        first = await service.evaluate(account)
        second = await service.evaluate(account)

        assert len(first.created) >= 1
        assert second.created == []
        assert second.suppressed_duplicate >= 1
        total = await db_session.scalar(select(func.count()).select_from(Alert))
        assert total == len(first.created)

    async def test_cooldown_suppresses_a_different_event_from_the_same_rule(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        await seed(db_session, account, days=21, per_day_gain=10, final_day_gain=400)
        service = AlertService(db_session)
        await service.evaluate(account)

        # A genuinely new event: same rule, different day.
        alert = await db_session.scalar(
            select(Alert).where(Alert.rule_key == AlertRuleKey.FOLLOWER_SPIKE)
        )
        assert alert is not None
        alert.dedupe_key = "FOLLOWER_SPIKE:1999-01-01"
        await db_session.flush()

        second = await service.evaluate(account)

        assert second.created == []
        assert second.suppressed_cooldown >= 1

    async def test_a_disabled_rule_does_not_fire(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        await seed(db_session, account, days=21, per_day_gain=10, final_day_gain=400)
        service = AlertService(db_session)

        configured = await service.rules_for(account.id)
        configured[AlertRuleKey.FOLLOWER_SPIKE].enabled = False
        await db_session.flush()

        result = await service.evaluate(account)
        assert all(a.rule_key is not AlertRuleKey.FOLLOWER_SPIKE for a in result.created)

    async def test_stateful_alerts_resolve_when_the_condition_clears(
        self, db_session: AsyncSession
    ) -> None:
        """'Collection has stopped' must disappear when collection resumes."""
        account = await make_account(db_session)
        await seed(db_session, account, days=21)
        service = AlertService(db_session)

        stale = Alert(
            x_account_id=account.id,
            rule_key=AlertRuleKey.COLLECTION_STALLED,
            severity=AlertSeverity.CRITICAL,
            state=AlertState.FIRING,
            title="Collection has not run for 40 hours",
            body="…",
            dedupe_key="COLLECTION_STALLED",
            fired_at=NOW - timedelta(hours=40),
        )
        db_session.add(stale)
        await db_session.flush()

        result = await service.evaluate(account)

        await db_session.refresh(stale)
        assert result.resolved >= 1
        assert stale.state is AlertState.RESOLVED
        assert stale.resolved_at is not None

    async def test_point_in_time_alerts_never_resolve_themselves(
        self, db_session: AsyncSession
    ) -> None:
        """'You gained 400 followers on Tuesday' is not a condition that can end."""
        account = await make_account(db_session)
        await seed(db_session, account, days=21)

        spike = Alert(
            x_account_id=account.id,
            rule_key=AlertRuleKey.FOLLOWER_SPIKE,
            severity=AlertSeverity.INFO,
            state=AlertState.FIRING,
            title="Unusual follower growth",
            body="…",
            dedupe_key="FOLLOWER_SPIKE:1999-01-01",
            fired_at=NOW - timedelta(days=3),
        )
        db_session.add(spike)
        await db_session.flush()

        await AlertService(db_session).evaluate(account)

        await db_session.refresh(spike)
        assert spike.state is AlertState.FIRING

    async def test_a_resolved_condition_that_returns_reopens_the_same_alert(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        await seed(db_session, account, days=21)
        service = AlertService(db_session)

        resolved = Alert(
            x_account_id=account.id,
            rule_key=AlertRuleKey.BUDGET_PRESSURE,
            severity=AlertSeverity.WARNING,
            state=AlertState.RESOLVED,
            title="API budget is critical",
            body="…",
            dedupe_key="BUDGET_PRESSURE",
            fired_at=NOW - timedelta(days=2),
            resolved_at=NOW - timedelta(days=1),
        )
        db_session.add(resolved)
        await db_session.flush()

        # Force the condition back on.
        original = service.build_context

        async def exhausted(account_arg: XAccount, *, days: int = 30) -> AlertContext:
            ctx = await original(account_arg, days=days)
            ctx.budget_state = "exhausted"
            ctx.budget_fraction = 1.2
            return ctx

        service.build_context = exhausted  # type: ignore[method-assign]
        await service.evaluate(account)

        await db_session.refresh(resolved)
        assert resolved.state is AlertState.FIRING
        assert resolved.resolved_at is None
        # The original firing time is kept: it records when the condition began.
        assert resolved.fired_at.replace(tzinfo=UTC) < NOW - timedelta(days=1)

    async def test_acknowledging_does_not_resolve(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        user = await db_session.get(User, account.user_id)
        assert user is not None

        alert = Alert(
            x_account_id=account.id,
            rule_key=AlertRuleKey.COLLECTION_STALLED,
            severity=AlertSeverity.CRITICAL,
            state=AlertState.FIRING,
            title="Collection has stopped",
            body="…",
            dedupe_key="COLLECTION_STALLED",
            fired_at=NOW,
        )
        db_session.add(alert)
        await db_session.flush()

        await AlertService(db_session).acknowledge(alert, user.id)

        assert alert.state is AlertState.ACKNOWLEDGED
        assert alert.resolved_at is None

    async def test_severity_floor_records_without_delivering(
        self, db_session: AsyncSession
    ) -> None:
        """The row is cheap; the interruption is not."""
        account = await make_account(db_session)
        await seed(db_session, account, days=21, per_day_gain=10, final_day_gain=400)
        service = AlertService(db_session)

        configured = await service.rules_for(account.id)
        for row in configured.values():
            row.min_severity = AlertSeverity.CRITICAL
        await db_session.flush()

        result = await service.evaluate(account)

        assert result.created
        assert result.delivered == 0
        assert await db_session.scalar(select(func.count()).select_from(Delivery)) == 0

    async def test_every_delivered_alert_records_its_delivery(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        await seed(db_session, account, days=21, per_day_gain=10, final_day_gain=400)

        result = await AlertService(db_session).evaluate(account)
        assert result.created

        deliveries = list(await db_session.scalars(select(Delivery)))
        assert deliveries
        dashboard = [d for d in deliveries if d.channel is DeliveryChannel.DASHBOARD]
        email = [d for d in deliveries if d.channel is DeliveryChannel.EMAIL]
        assert all(d.status is DeliveryStatus.SENT for d in dashboard)
        # SMTP is unconfigured in tests: skipped, not failed. Nothing went wrong.
        assert all(d.status is DeliveryStatus.SKIPPED for d in email)


# ----------------------------------------------------------------- channels


class TestChannels:
    def test_addresses_are_redacted_in_the_delivery_log(self) -> None:
        assert redact_address("owner@example.com") == "o***@example.com"
        assert redact_address("nonsense") == "***"

    async def test_unconfigured_email_is_skipped_not_failed(self) -> None:
        channel = EmailChannel()
        assert channel.is_configured is False

        result = await channel.send(Message(subject="x", text="y"))
        assert result.status is DeliveryStatus.SKIPPED
        assert result.error and "not configured" in result.error

    def test_dashboard_is_always_included(self) -> None:
        """An alert that exists nowhere is not an alert."""
        assert any(c.channel is DeliveryChannel.DASHBOARD for c in build_channels([]))
        assert any(c.channel is DeliveryChannel.DASHBOARD for c in build_channels(["EMAIL"]))

    def test_unknown_channel_names_are_ignored(self) -> None:
        channels = build_channels(["SMOKE_SIGNAL"])
        assert [c.channel for c in channels] == [DeliveryChannel.DASHBOARD]


# ------------------------------------------------------------------ reports


class TestReports:
    async def test_report_states_its_data_coverage(self, db_session: AsyncSession) -> None:
        """A report covering 60% of a period reads exactly like one covering all of it."""
        account = await make_account(db_session)
        await seed(db_session, account, days=14)

        report = await ReportService(db_session).generate(
            account, ReportPeriod.WEEKLY, deliver=False
        )

        keys = [section["key"] for section in report.sections]
        assert "data_quality" in keys
        assert "followers" in keys

    async def test_revenue_section_is_absent_when_nothing_is_recorded(
        self, db_session: AsyncSession
    ) -> None:
        """Better absent than a section confidently reporting zero earnings."""
        account = await make_account(db_session)
        await seed(db_session, account, days=10)

        report = await ReportService(db_session).generate(
            account, ReportPeriod.DAILY, deliver=False
        )
        assert "revenue" not in [s["key"] for s in report.sections]

    async def test_revenue_section_declares_it_was_never_measured(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        await seed(db_session, account, days=10)
        db_session.add(
            RevenueEntry(
                x_account_id=account.id,
                source_type=RevenueSourceType.SPONSORSHIP,
                amount_minor=50_000,
                currency="USD",
                earned_at=date.today(),
                provenance=Provenance.USER_ENTERED,
                external_ref="rep-1",
            )
        )
        await db_session.flush()

        report = await ReportService(db_session).generate(
            account, ReportPeriod.MONTHLY, deliver=False
        )
        revenue = next(s for s in report.sections if s["key"] == "revenue")

        assert revenue["provenance"] == Provenance.USER_ENTERED.value
        assert any("no creator-earnings API" in c for c in revenue["caveats"])

    async def test_regenerating_a_period_amends_rather_than_duplicates(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        await seed(db_session, account, days=14)
        service = ReportService(db_session)

        first = await service.generate(account, ReportPeriod.WEEKLY, deliver=False)
        second = await service.generate(account, ReportPeriod.WEEKLY, deliver=False)

        assert first.id == second.id
        assert await db_session.scalar(select(func.count()).select_from(Report)) == 1

    async def test_a_failed_email_does_not_unmake_the_report(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        await seed(db_session, account, days=10)

        report = await ReportService(db_session).generate(account, ReportPeriod.DAILY, deliver=True)

        # Dashboard succeeds, email is skipped — the report is still delivered
        # and readable.
        assert report.sections
        deliveries = list(await db_session.scalars(select(Delivery)))
        assert {d.channel for d in deliveries} == {DeliveryChannel.DASHBOARD, DeliveryChannel.EMAIL}

    async def test_text_rendering_includes_provenance(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        await seed(db_session, account, days=10)
        report = await ReportService(db_session).generate(
            account, ReportPeriod.DAILY, deliver=False
        )

        text = render_text(report)
        assert report.title in text
        assert "[MEASURED]" in text

    async def test_an_empty_account_still_produces_a_report(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        report = await ReportService(db_session).generate(
            account, ReportPeriod.WEEKLY, deliver=False
        )
        assert "nothing to report" in report.summary


# ---------------------------------------------------------------------- API


class TestAlertsApi:
    async def test_endpoints_require_authentication(self, client) -> None:  # type: ignore[no-untyped-def]
        account_id = uuid.uuid4()
        for path in ("", "/rules", "/reports", "/deliveries", "/channels"):
            response = await client.get(f"/api/v1/alerts/{account_id}{path}")
            assert response.status_code == 401

    async def test_another_users_account_is_not_visible(self, registered_client) -> None:  # type: ignore[no-untyped-def]
        response = await registered_client.get(f"/api/v1/alerts/{uuid.uuid4()}/rules")
        assert response.status_code == 404

    async def test_acknowledging_someone_elses_alert_is_not_found(
        self,
        registered_client,  # type: ignore[no-untyped-def]
    ) -> None:
        response = await registered_client.post(f"/api/v1/alerts/{uuid.uuid4()}/acknowledge")
        assert response.status_code == 404


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(1.0, None), (7.0, AlertSeverity.WARNING), (30.0, AlertSeverity.CRITICAL)],
)
def test_stall_thresholds(hours: float, expected: AlertSeverity | None) -> None:
    """Pure threshold check, with no database behind it."""
    ctx = AlertContext(
        username="x",
        now=NOW,
        summary=_empty_summary(),
        series=_empty_series(),
        performances=[],
        hours_since_snapshot=hours,
        posts_awaiting_freeze=0,
        budget_state="healthy",
        budget_fraction=0.1,
        unavailable_capabilities=[],
        refresh_failure_count=0,
    )
    finding = rules.detect_collection_stalled(ctx)
    assert (finding.severity if finding else None) is expected


def _empty_summary() -> DashboardSummary:
    return DashboardSummary(followers=None, hours_of_history=0, posts=0, window_days=30)


def _empty_series() -> FollowerSeries:
    return FollowerSeries()
