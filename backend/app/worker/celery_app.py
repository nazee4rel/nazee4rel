"""Celery application and the beat schedule.

The cadence here is the operational expression of the 30-day cliff:

* Follower snapshots run hourly, forever. One resource each (~$0.72/month), and
  hourly resolution is the minimum that lets Phase 5 separate one post's effect
  on follower growth from another's.
* Post metrics run every 15 minutes. That is not the snapshot rate — the
  per-post decay ladder in `schedule.py` decides what is actually due. Waking
  frequently just means a post crossing into its freeze window is picked up
  promptly rather than up to an hour late.
* The freeze sweep runs hourly as a dedicated safety net, so a post nearing the
  cliff gets a second, independent chance even if the ordinary cycle is
  degraded or failing.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "xagent",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Collection is I/O-bound and idempotent by design (snapshots are keyed by
    # timestamp), so late acknowledgement is safe and a worker crash mid-run
    # simply re-runs the cycle.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=600,
    task_soft_time_limit=540,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
)

celery_app.conf.beat_schedule = {
    "account-snapshot-hourly": {
        "task": "collect.account_snapshots",
        # Just after the hour, so the snapshot lands in the hour it measures.
        "schedule": crontab(minute="1"),
    },
    "discover-posts": {
        "task": "collect.discover_posts",
        "schedule": crontab(minute="*/30"),
    },
    "post-metrics": {
        "task": "collect.post_metrics",
        "schedule": crontab(minute="*/15"),
    },
    "freeze-sweep": {
        "task": "collect.freeze_sweep",
        "schedule": crontab(minute="20"),
    },
    "capability-probe-weekly": {
        "task": "collect.probe_capabilities",
        "schedule": crontab(hour="4", minute="30", day_of_week="1"),
    },
    "cleanup-oauth-states": {
        "task": "maintenance.cleanup_oauth_states",
        "schedule": crontab(hour="3", minute="0"),
    },
    # Collection stopping silently is indistinguishable from an account that
    # simply is not posting — until a month of unrecoverable impressions is
    # already gone. Worth an explicit check.
    "stale-account-check": {
        "task": "maintenance.stale_account_check",
        "schedule": crontab(minute="45"),
    },
    # The agent cycle. Once a day, early enough that a morning report has
    # something to say — the metrics it reasons about move on the scale of
    # days, so a tighter cadence would pay for model calls to observe noise.
    "agent-daily-cycle": {
        "task": "agent.daily_cycle",
        "schedule": crontab(hour="6", minute="15"),
    },
    # Grading runs separately as well, so accountability for past advice does
    # not depend on the reasoning step being available today.
    "agent-verify-predictions": {
        "task": "agent.verify_predictions",
        "schedule": crontab(hour="7", minute="5"),
    },
    "agent-expire-approvals": {
        "task": "agent.expire_approvals",
        "schedule": crontab(minute="10"),
    },
}
