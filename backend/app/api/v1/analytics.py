"""Analytics endpoints.

Response shapes deliberately carry their own caveats — sample sizes, provenance,
and the reason a value is missing. The frontend should never have to infer
whether a number is trustworthy, and neither should the model in Phase 6.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, File, Query, UploadFile
from sqlalchemy import select

from app.analytics import baselines
from app.api.deps import CurrentUser, DbSession
from app.core.errors import NotFoundError, ValidationError
from app.models.enums import Provenance
from app.models.revenue import RevenueSourceType
from app.models.x_account import XAccount
from app.services.analytics_service import AnalyticsService
from app.services.revenue_import import RevenueImporter

router = APIRouter(prefix="/analytics", tags=["analytics"])

MAX_CSV_BYTES = 5 * 1024 * 1024

# Hoisted so ruff's B008 (function call in a default) does not fire on what is
# the idiomatic FastAPI spelling.
CSV_UPLOAD = File(...)


async def _account(db: DbSession, user: CurrentUser, account_id: uuid.UUID) -> XAccount:
    account = await db.scalar(
        select(XAccount).where(XAccount.id == account_id, XAccount.user_id == user.id)
    )
    if account is None:
        raise NotFoundError("X account not found.")
    return account


def _rate_payload(rate: Any) -> dict[str, Any]:
    return {
        "value": rate.value,
        "percent": rate.percent,
        "denominator": rate.denominator.value,
        "denominator_value": rate.denominator_value,
        "provenance": rate.provenance.value,
        "description": rate.describe(),
    }


@router.get("/{account_id}/overview")
async def overview(account_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Any:
    account = await _account(db, user, account_id)
    service = AnalyticsService(db)

    growth = await service.growth(account.id)
    score = await service.growth_score(account.id)

    return {
        "followers": growth.current_followers,
        "hours_of_history": growth.hours_of_history,
        "growth_baseline": (
            {
                "median_daily": growth.baseline.median,
                "observations": growth.baseline.observations,
                "is_reliable": growth.baseline.is_reliable,
                "note": growth.baseline.note,
            }
            if growth.baseline
            else None
        ),
        "latest_anomaly": (
            {
                "direction": growth.latest_anomaly.direction.value,
                "severity": growth.latest_anomaly.severity.value,
                "z_score": growth.latest_anomaly.z_score,
                "explanation": growth.latest_anomaly.explanation,
            }
            if growth.latest_anomaly
            else None
        ),
        "trend": (
            {
                "direction": growth.trend.direction,
                "change_ratio": growth.trend.change_ratio,
                "explanation": growth.trend.explanation,
                "is_reliable": growth.trend.is_reliable,
            }
            if growth.trend
            else None
        ),
        "daily_deltas": growth.daily_deltas,
        "growth_score": score,
        "caveats": growth.caveats,
    }


@router.get("/{account_id}/posts")
async def post_performance(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=30, ge=1, le=365),
) -> Any:
    account = await _account(db, user, account_id)
    ranked = await AnalyticsService(db).top_and_bottom(account.id, days)

    def serialise(post: Any) -> dict[str, Any]:
        return {
            "post_id": post.post_id,
            "x_post_id": post.x_post_id,
            "text": post.text,
            "posted_at": post.posted_at,
            "engagement": post.engagement,
            "weighted_engagement": post.weighted_engagement,
            "impressions": post.impressions,
            "impressions_available": post.impressions_available,
            "unavailable_reason": post.unavailable_reason,
            "engagement_rate": _rate_payload(post.engagement_rate),
            "percentile": post.percentile,
            "format": {
                "has_media": post.has_media,
                "has_link": post.has_link,
                "is_thread": post.is_thread,
                "char_count": post.char_count,
                "post_type": post.post_type,
            },
        }

    return {
        "top": [serialise(p) for p in ranked["top"]],
        "bottom": [serialise(p) for p in ranked["bottom"]],
        "total_posts": ranked["total_posts"],
        "comparable_posts": ranked["comparable_posts"],
        # Ranking only across posts with a usable denominator keeps the
        # comparison fair; the count of exclusions is surfaced rather than hidden.
        "excluded_no_impressions": ranked["excluded_no_impressions"],
    }


@router.get("/{account_id}/attribution")
async def follower_attribution(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=30, ge=7, le=90),
) -> Any:
    """Which posts plausibly drove follower growth.

    Modelled, never measured — X exposes no follower-event data. Every figure
    carries a confidence interval, and the report says plainly when the data
    cannot separate one post's effect from another's.
    """
    account = await _account(db, user, account_id)
    report = await AnalyticsService(db).follower_attribution(account.id, days)

    return {
        "provenance": report.provenance.value,
        "confidence": report.confidence.value,
        "is_usable": report.is_usable,
        "summary": report.summary(),
        "hours_of_history": report.hours_of_history,
        "baseline_per_hour": report.baseline_per_hour,
        "total_observed_growth": report.total_observed_growth,
        "total_explained": report.total_explained,
        "unexplained": report.unexplained,
        "caveats": report.caveats,
        "contributions": [
            {
                "post_id": c.post_id,
                "posted_at": c.posted_at,
                "estimate": round(c.estimate, 2),
                "ci_low": round(c.ci_low, 2),
                "ci_high": round(c.ci_high, 2),
                "share_of_explained": round(c.share_of_explained, 4),
                "is_distinguishable": c.is_distinguishable,
            }
            for c in report.contributions
        ],
    }


@router.get("/{account_id}/timing")
async def timing(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=90, ge=7, le=365),
) -> Any:
    account = await _account(db, user, account_id)
    analysis = await AnalyticsService(db).timing(account.id, days)

    def bucket(b: Any) -> dict[str, Any]:
        return {
            "label": b.label,
            "sample_size": b.sample_size,
            "median_performance": b.median_performance,
            "is_reliable": b.is_reliable,
            "status": b.status,
        }

    return {
        "timezone": analysis.timezone,
        "total_posts": analysis.total_posts,
        "is_reliable": analysis.is_reliable,
        "recommendation": analysis.recommendation(),
        "by_hour": [bucket(b) for b in analysis.by_hour],
        "by_weekday": [bucket(b) for b in analysis.by_weekday],
        "best_hours": [bucket(b) for b in analysis.best_hours],
        "worst_hours": [bucket(b) for b in analysis.worst_hours],
        "caveats": analysis.caveats,
    }


@router.get("/{account_id}/formats")
async def formats(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=90, ge=7, le=365),
) -> Any:
    account = await _account(db, user, account_id)
    analysis = await AnalyticsService(db).formats(account.id, days)

    return {
        "total_posts": analysis.total_posts,
        "is_reliable": analysis.is_reliable,
        "groups": [
            {
                "label": g.label,
                "sample_size": g.sample_size,
                "median_performance": g.median_performance,
                "is_reliable": g.is_reliable,
                "status": g.status,
            }
            for g in analysis.groups
        ],
        "best": analysis.best.label if analysis.best else None,
        "worst": analysis.worst.label if analysis.worst else None,
        "caveats": analysis.caveats,
    }


@router.get("/{account_id}/revenue")
async def revenue(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    currency: str = Query(default="USD", min_length=3, max_length=3),
) -> Any:
    account = await _account(db, user, account_id)
    data = await AnalyticsService(db).revenue(account.id, currency.upper())
    summary = data["summary"]

    return {
        "provenance": data["provenance"].value,
        "note": data["note"],
        "currency": summary.currency,
        "total_minor": summary.total_minor,
        "total": summary.total,
        "entry_count": summary.entry_count,
        "growth_ratio": summary.growth_ratio,
        "by_source": [
            {
                "source_type": s.source_type.value,
                "total_minor": s.total_minor,
                "entry_count": s.entry_count,
                "share": round(s.share, 4),
            }
            for s in summary.by_source
        ],
        "by_month": [
            {"month": m.month, "total_minor": m.total_minor, "entry_count": m.entry_count}
            for m in summary.by_month
        ],
        "best_source": summary.best_source.source_type.value if summary.best_source else None,
        "per_post": [
            {
                "post_id": p.post_id,
                "revenue_minor": p.revenue_minor,
                "impressions": p.impressions,
                "rpm_minor": p.rpm_minor,
                "rpm_available": p.rpm_available,
                # RPM is suppressed rather than approximated where impressions
                # were never collected.
                "reason_unavailable": p.reason_unavailable,
            }
            for p in data["per_post"]
        ],
        "per_campaign": [
            {
                "campaign_id": c.campaign_id,
                "revenue_minor": c.revenue_minor,
                "contracted_minor": c.contracted_minor,
                "fulfilment_ratio": c.fulfilment_ratio,
                "entry_count": c.entry_count,
            }
            for c in data["per_campaign"]
        ],
        "caveats": summary.caveats,
    }


@router.post("/{account_id}/revenue/import")
async def import_revenue_csv(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    file: UploadFile = CSV_UPLOAD,
    default_source: str = Query(default="OTHER"),
    default_currency: str = Query(default="USD", min_length=3, max_length=3),
) -> Any:
    """Import a revenue CSV.

    Rows are fingerprinted, so importing the same statement twice adds nothing
    rather than doubling your reported revenue. Unreadable rows are reported
    individually instead of being coerced to zero.
    """
    account = await _account(db, user, account_id)

    raw = await file.read()
    if len(raw) > MAX_CSV_BYTES:
        raise ValidationError(f"File exceeds the {MAX_CSV_BYTES // (1024 * 1024)}MB limit.")

    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("The file is not valid UTF-8. Re-export it as UTF-8 CSV.") from None

    try:
        source = RevenueSourceType(default_source.upper())
    except ValueError:
        raise ValidationError(
            f"Unknown source type {default_source!r}. Valid values: "
            f"{', '.join(s.value for s in RevenueSourceType)}."
        ) from None

    result = await RevenueImporter(db).import_csv(
        account.id,
        content,
        default_source=source,
        default_currency=default_currency.upper(),
    )

    return {
        "batch_id": result.batch_id,
        "summary": result.summary(),
        "imported": result.imported,
        "duplicates": result.duplicates,
        "rows_seen": result.rows_seen,
        "total_minor": result.total_minor,
        "detected_columns": result.detected_columns,
        "errors": [{"row": e.row_number, "reason": e.reason} for e in result.errors[:50]],
    }


@router.get("/{account_id}/summary")
async def dashboard_summary(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=30, ge=7, le=365),
) -> Any:
    """Everything the Overview headline row needs, in one read.

    Assembled server-side so the figures on that row cannot disagree with each
    other, which they can when a page issues six requests and the collector
    writes between two of them.
    """
    account = await _account(db, user, account_id)
    summary = await AnalyticsService(db).dashboard_summary(account.id, days)

    return {
        "window_days": summary.window_days,
        "followers": {
            "value": summary.followers,
            "change_7d": summary.followers_change_7d,
            "change_note": summary.followers_change_note,
            "median_daily_change": summary.median_daily_change,
            "baseline_reliable": summary.baseline_reliable,
            "hours_of_history": summary.hours_of_history,
            "provenance": Provenance.MEASURED.value,
        },
        "engagement": {
            "median_rate": summary.engagement_rate_median,
            "basis": summary.engagement_rate_basis,
            "sample_size": summary.engagement_rate_sample,
            "provenance": (
                Provenance.DERIVED.value
                if summary.engagement_rate_median is not None
                else Provenance.UNAVAILABLE.value
            ),
        },
        "impressions": {
            # Null rather than 0 when nothing was collected — the difference
            # between "no reach" and "never measured" is the whole project.
            "total": summary.impressions_total,
            "posts_counted": summary.impressions_posts,
            "posts_missing": summary.impressions_posts_missing,
            "provenance": (
                Provenance.MEASURED.value
                if summary.impressions_total is not None
                else Provenance.UNAVAILABLE.value
            ),
        },
        "revenue": {
            "total_minor": summary.revenue_total_minor,
            "currency": summary.revenue_currency,
            "entry_count": summary.revenue_entries,
            # Never MEASURED: X publishes no creator-earnings API.
            "provenance": Provenance.USER_ENTERED.value,
        },
        "posts": summary.posts,
        "growth_score": summary.growth_score,
        "trend": (
            {
                "direction": summary.trend.direction,
                "change_ratio": summary.trend.change_ratio,
                "explanation": summary.trend.explanation,
                "is_reliable": summary.trend.is_reliable,
            }
            if summary.trend
            else None
        ),
        "latest_anomaly": (
            {
                "direction": summary.latest_anomaly.direction.value,
                "severity": summary.latest_anomaly.severity.value,
                "z_score": summary.latest_anomaly.z_score,
                "explanation": summary.latest_anomaly.explanation,
            }
            if summary.latest_anomaly
            else None
        ),
        "top_post": (
            {
                "post_id": summary.top_post.post_id,
                "x_post_id": summary.top_post.x_post_id,
                "text": summary.top_post.text,
                "posted_at": summary.top_post.posted_at,
                "engagement": summary.top_post.engagement,
                "impressions": summary.top_post.impressions,
                "engagement_rate": _rate_payload(summary.top_post.engagement_rate),
            }
            if summary.top_post
            else None
        ),
        "caveats": summary.caveats,
    }


@router.get("/{account_id}/series")
async def follower_series(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=30, ge=1, le=365),
) -> Any:
    """The follower series for charting, with gaps preserved as gaps."""
    account = await _account(db, user, account_id)
    series = await AnalyticsService(db).follower_series(account.id, days)

    return {
        "provenance": series.provenance.value,
        "points": [{"at": p.at, "followers": p.followers} for p in series.points],
        "daily": [
            {
                "day": d.day,
                "followers": d.followers,
                "delta": d.delta,
                "posts": d.posts,
                # The client must not plot an unobserved day as zero.
                "observed": d.observed,
            }
            for d in series.daily
        ],
        "gaps": [{"start": g.start, "end": g.end, "hours": g.hours} for g in series.gaps],
        "caveats": series.caveats,
    }


@router.get("/{account_id}/topics")
async def topic_performance(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=90, ge=7, le=365),
) -> Any:
    """Topic performance over posts the agent has classified."""
    account = await _account(db, user, account_id)
    analysis = await AnalyticsService(db).topic_performance(account.id, days)

    return {
        "topics": [
            {
                "topic": t.topic,
                "posts": t.posts,
                "median_engagement_rate": t.median_engagement_rate,
                "share_of_classified": t.share_of_classified,
                "is_reliable": t.is_reliable,
                "status": t.status,
            }
            for t in analysis.topics
        ],
        "total_posts": analysis.total_posts,
        "classified_posts": analysis.classified_posts,
        "unclassified_posts": analysis.unclassified_posts,
        "best": analysis.best,
        "worst": analysis.worst,
        "is_reliable": analysis.is_reliable,
        "provenance": analysis.provenance.value,
        "caveats": analysis.caveats,
    }


WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@router.get("/{account_id}/seasonality")
async def seasonality(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=90, ge=14, le=365),
) -> Any:
    """Weekday rhythm for follower change and for engagement, kept separate."""
    account = await _account(db, user, account_id)
    analysis = await AnalyticsService(db).seasonality(account.id, days)

    def profile(p: Any) -> dict[str, Any]:
        return {
            "is_reliable": p.is_reliable,
            "days": [
                {
                    "weekday": index,
                    "name": WEEKDAY_NAMES[index],
                    # Absent rather than zero where the weekday lacks enough
                    # observations to have a median at all.
                    "median": p.by_weekday.get(index),
                    "observations": p.observations_by_weekday.get(index, 0),
                }
                for index in range(7)
            ],
        }

    return {
        "follower_change": profile(analysis.follower_profile),
        "engagement_rate": profile(analysis.engagement_profile),
        "follower_days_observed": analysis.follower_days_observed,
        "posts_observed": analysis.posts_observed,
        "minimum_per_weekday": baselines.MIN_PER_WEEKDAY,
        "caveats": analysis.caveats,
    }
