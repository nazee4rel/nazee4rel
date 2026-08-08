"""The VERIFY stage: grading past advice against what actually happened.

A recommendation is stored as a prediction — a metric, a direction, a baseline
measured at the time it was made, and a date after which it is fair to check.
This module measures the metric again and grades the prediction, and those
grades are shown to the model on later runs.

Three judgements are made carefully rather than conveniently:

* **Advice nobody took cannot be graded.** A dismissed recommendation is
  INCONCLUSIVE, never REFUTED. Scoring unfollowed advice as wrong would teach
  the wrong lesson and would flatter the system whenever the owner happened to
  improve anyway.
* **The baseline is measured, not asserted.** It is computed here at proposal
  time over the same window length used for grading. If the model supplied its
  own baseline it would be marking its own homework.
* **A small move is not a result.** Changes inside the material-change band are
  INCONCLUSIVE, because account metrics wander. Only a move large enough to
  outrun ordinary variation counts either way.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.agent import (
    PredictedDirection,
    PredictedMetric,
    Recommendation,
    RecommendationStatus,
    VerificationResult,
)
from app.models.content import AccountMetricSnapshot, Post
from app.services.analytics_service import AnalyticsService

log = get_logger(__name__)

# Relative move that counts as a real change. Below this the metric has not
# moved so much as drifted.
MATERIAL_CHANGE = 0.10
# Below this many observations in the verification window there is nothing to
# take a median of.
MIN_OBSERVATIONS = 5
# Absolute floor for metrics whose baseline is zero, so "0 -> 0.4 followers a
# day" is not graded as an infinite improvement.
NEAR_ZERO = 1e-9


@dataclass(frozen=True)
class Grade:
    result: VerificationResult
    observed: float | None
    note: str


def grade_prediction(
    *,
    direction: PredictedDirection,
    baseline: float | None,
    observed: float | None,
    observations: int,
    adopted: bool,
) -> Grade:
    """Grade one prediction. Pure, so the rules are testable in isolation."""
    if not adopted:
        return Grade(
            VerificationResult.INCONCLUSIVE,
            observed,
            "You dismissed this recommendation, so what happened afterwards cannot "
            "confirm or refute it.",
        )

    if baseline is None:
        return Grade(
            VerificationResult.INCONCLUSIVE,
            observed,
            "No baseline could be measured when this was proposed, so there is nothing "
            "to compare against.",
        )

    if observed is None or observations < MIN_OBSERVATIONS:
        return Grade(
            VerificationResult.INCONCLUSIVE,
            observed,
            f"Only {observations} observation(s) fell inside the verification window; "
            f"{MIN_OBSERVATIONS} are needed before a median means anything.",
        )

    if abs(baseline) <= NEAR_ZERO:
        moved = abs(observed) > NEAR_ZERO
        change_text = f"from zero to {observed:.4g}"
    else:
        ratio = (observed - baseline) / abs(baseline)
        moved = abs(ratio) >= MATERIAL_CHANGE
        change_text = f"{baseline:.4g} to {observed:.4g} ({ratio:+.1%})"

    if direction is PredictedDirection.MAINTAIN:
        if moved:
            return Grade(
                VerificationResult.REFUTED,
                observed,
                f"The prediction was that this would hold steady; it moved {change_text}.",
            )
        return Grade(
            VerificationResult.CONFIRMED,
            observed,
            f"Held steady as predicted: {change_text}.",
        )

    if not moved:
        return Grade(
            VerificationResult.INCONCLUSIVE,
            observed,
            f"The metric went {change_text} — inside the band where account metrics "
            f"drift anyway, so this neither confirms nor refutes the prediction.",
        )

    increased = observed > baseline
    predicted_increase = direction is PredictedDirection.INCREASE
    if increased == predicted_increase:
        return Grade(
            VerificationResult.CONFIRMED,
            observed,
            f"Moved as predicted: {change_text}.",
        )
    return Grade(
        VerificationResult.REFUTED,
        observed,
        f"Moved against the prediction: {change_text}.",
    )


@dataclass(frozen=True)
class Measurement:
    value: float | None
    observations: int
    note: str = ""


class MetricMeasurer:
    """Measures the metrics predictions may be written about.

    The set is small on purpose: a prediction the system cannot re-measure is
    unfalsifiable, so `PredictedMetric` only offers what appears here.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.analytics = AnalyticsService(db)

    async def measure(
        self,
        metric: PredictedMetric,
        x_account_id: uuid.UUID,
        *,
        since: datetime,
        until: datetime | None = None,
    ) -> Measurement:
        until = until or datetime.now(UTC)
        match metric:
            case PredictedMetric.FOLLOWER_GROWTH_DAILY:
                return await self._follower_growth(x_account_id, since, until)
            case PredictedMetric.ENGAGEMENT_RATE_MEDIAN:
                return await self._post_median(x_account_id, since, until, "rate")
            case PredictedMetric.IMPRESSIONS_MEDIAN:
                return await self._post_median(x_account_id, since, until, "impressions")
            case PredictedMetric.POSTS_PER_WEEK:
                return await self._posts_per_week(x_account_id, since, until)

    async def _follower_growth(
        self, x_account_id: uuid.UUID, since: datetime, until: datetime
    ) -> Measurement:
        snapshots = list(
            await self.db.scalars(
                select(AccountMetricSnapshot)
                .where(
                    AccountMetricSnapshot.x_account_id == x_account_id,
                    AccountMetricSnapshot.captured_at >= since,
                    AccountMetricSnapshot.captured_at <= until,
                )
                .order_by(AccountMetricSnapshot.captured_at)
            )
        )
        by_day: dict[str, int] = {}
        for snapshot in snapshots:
            day = snapshot.captured_at.strftime("%Y-%m-%d")
            by_day[day] = max(by_day.get(day, 0), snapshot.followers_count)

        days = sorted(by_day)
        deltas = [float(by_day[b] - by_day[a]) for a, b in zip(days, days[1:], strict=False)]
        if not deltas:
            return Measurement(None, 0, "No follower history in this window.")
        return Measurement(statistics.median(deltas), len(deltas))

    async def _post_median(
        self, x_account_id: uuid.UUID, since: datetime, until: datetime, which: str
    ) -> Measurement:
        days = max(int((until - since).total_seconds() / 86400), 1)
        performances = await self.analytics.post_performance(x_account_id, days=days)
        in_window = [p for p in performances if since <= _aware(p.posted_at) <= until]

        if which == "impressions":
            values = [float(p.impressions) for p in in_window if p.impressions is not None]
            missing = len(in_window) - len(values)
            note = (
                f"{missing} post(s) in this window have no impression figure and were "
                f"excluded rather than counted as zero."
                if missing
                else ""
            )
        else:
            values = [
                float(p.engagement_rate.value)
                for p in in_window
                if p.engagement_rate.value is not None
            ]
            note = ""

        if not values:
            return Measurement(None, 0, note or "No comparable posts in this window.")
        return Measurement(statistics.median(values), len(values), note)

    async def _posts_per_week(
        self, x_account_id: uuid.UUID, since: datetime, until: datetime
    ) -> Measurement:
        posts = list(
            await self.db.scalars(
                select(Post).where(
                    Post.x_account_id == x_account_id,
                    Post.posted_at >= since,
                    Post.posted_at <= until,
                )
            )
        )
        weeks = max((until - since).total_seconds() / (7 * 86400), 1 / 7)
        # Each post is its own observation here, unlike the median metrics.
        return Measurement(len(posts) / weeks, len(posts))


class VerificationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.measurer = MetricMeasurer(db)

    async def measure_baseline(
        self,
        metric: PredictedMetric,
        x_account_id: uuid.UUID,
        *,
        window_days: int,
    ) -> Measurement:
        """Baseline over the window immediately before the recommendation.

        Same length as the verification window, so the two are comparable.
        """
        now = datetime.now(UTC)
        return await self.measurer.measure(
            metric, x_account_id, since=now - timedelta(days=window_days), until=now
        )

    async def grade_due(
        self, x_account_id: uuid.UUID, *, today: date | None = None
    ) -> list[Recommendation]:
        """Grade every recommendation whose verification date has arrived."""
        today = today or datetime.now(UTC).date()
        due = list(
            await self.db.scalars(
                select(Recommendation).where(
                    Recommendation.x_account_id == x_account_id,
                    Recommendation.verification_result == VerificationResult.PENDING,
                    Recommendation.verify_after <= today,
                )
            )
        )

        graded: list[Recommendation] = []
        for recommendation in due:
            created = _aware(recommendation.created_at)
            measurement = await self.measurer.measure(
                recommendation.predicted_metric,
                x_account_id,
                since=created,
            )
            grade = grade_prediction(
                direction=recommendation.predicted_direction,
                baseline=recommendation.baseline_value,
                observed=measurement.value,
                observations=measurement.observations,
                adopted=recommendation.status is not RecommendationStatus.DISMISSED,
            )
            recommendation.verification_result = grade.result
            recommendation.observed_value = grade.observed
            recommendation.verification_note = " ".join(
                part for part in (grade.note, measurement.note) if part
            )
            recommendation.verified_at = datetime.now(UTC)
            graded.append(recommendation)

        if graded:
            await self.db.flush()
            log.info("agent.verified", count=len(graded), account_id=str(x_account_id))
        return graded

    async def history_for_prompt(self, x_account_id: uuid.UUID, limit: int = 10) -> list[str]:
        """Graded recommendations, phrased for the next run's prompt.

        This is the whole point of the verify stage: without it the agent would
        propose the same thing every week regardless of whether it ever worked.
        """
        rows = list(
            await self.db.scalars(
                select(Recommendation)
                .where(
                    Recommendation.x_account_id == x_account_id,
                    Recommendation.verification_result != VerificationResult.PENDING,
                )
                .order_by(Recommendation.verified_at.desc())
                .limit(limit)
            )
        )
        return [
            f"[{row.verification_result.value}] {row.title} "
            f"(predicted {row.predicted_metric.value} would "
            f"{row.predicted_direction.value.lower()}). {row.verification_note or ''}".strip()
            for row in rows
        ]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
