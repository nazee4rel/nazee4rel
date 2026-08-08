"""The collectors.

Three jobs, each recorded as a `CollectionRun`:

* `collect_account_snapshot` — hourly follower count. One resource, ~$0.72/month,
  and the substrate the whole growth and attribution model rests on.
* `discover_posts` — find posts we have not seen, newest first.
* `collect_post_metrics` — walk the decay ladder from `schedule.py`, taking
  snapshots and, above all, never missing a final freeze.

A recurring principle: when something cannot be collected, that fact is written
down. A silent skip leaves a hole in the data with no explanation, and holes in
this dataset are permanent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors import schedule
from app.collectors.parsing import parse_account_metrics, parse_metrics, parse_post
from app.core.logging import get_logger
from app.integrations.x.client import XApiClient
from app.integrations.x.errors import (
    XApiError,
    XBudgetExceededError,
    XCapabilityUnavailableError,
    XRateLimitError,
)
from app.models.collection import CollectionKind, CollectionRun, CollectionStatus
from app.models.content import AccountMetricSnapshot, Post, PostMetricSnapshot
from app.models.enums import Provenance
from app.models.x_account import XAccount
from app.services.cost_service import CostService

log = get_logger(__name__)

# Cap on posts snapshotted in one cycle. Bounds both the cost of a single run
# and how long one account can monopolise a worker.
MAX_POSTS_PER_CYCLE = 100
# X's page size for the timeline endpoint.
PAGE_SIZE = 100


@dataclass
class CollectionResult:
    kind: CollectionKind
    status: CollectionStatus
    posts_discovered: int = 0
    snapshots_written: int = 0
    skipped_count: int = 0
    degraded_reason: str | None = None
    error: str | None = None
    details: dict[str, object] = field(default_factory=dict)


class Collector:
    def __init__(self, db: AsyncSession, client: XApiClient) -> None:
        self.db = db
        self.client = client
        self.account: XAccount = client.account
        self.cost = CostService(db)

    # ------------------------------------------------------------------ runs
    async def _begin(self, kind: CollectionKind) -> CollectionRun:
        run = CollectionRun(
            x_account_id=self.account.id,
            kind=kind,
            status=CollectionStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        self.db.add(run)
        await self.db.flush()
        return run

    async def _finish(self, run: CollectionRun, result: CollectionResult) -> CollectionRun:
        run.status = result.status
        run.finished_at = datetime.now(UTC)
        run.posts_discovered = result.posts_discovered
        run.snapshots_written = result.snapshots_written
        run.skipped_count = result.skipped_count
        run.degraded_reason = result.degraded_reason
        run.error = result.error
        run.details = result.details
        await self.db.flush()

        log.info(
            "collection.finished",
            kind=result.kind.value,
            status=result.status.value,
            snapshots=result.snapshots_written,
            discovered=result.posts_discovered,
            skipped=result.skipped_count,
        )
        return run

    async def _budget_state(self) -> schedule.BudgetState:
        status = await self.cost.budget_status(self.account.id)
        return schedule.BudgetState(str(status["state"]))

    # ------------------------------------------------- account snapshot
    async def collect_account_snapshot(self) -> CollectionRun:
        """Record the current follower count.

        X offers no follower history endpoint, so this hourly row *is* the
        history. Skipping it loses that hour permanently.
        """
        run = await self._begin(CollectionKind.ACCOUNT_SNAPSHOT)
        result = CollectionResult(CollectionKind.ACCOUNT_SNAPSHOT, CollectionStatus.RUNNING)

        try:
            response = await self.client.get_me()
        except (XCapabilityUnavailableError, XBudgetExceededError, XRateLimitError) as exc:
            result.status = CollectionStatus.SKIPPED
            result.error = str(exc)
            return await self._finish(run, result)
        except XApiError as exc:
            result.status = CollectionStatus.FAILED
            result.error = str(exc)
            return await self._finish(run, result)

        data = response.data if isinstance(response.data, dict) else {}
        metrics = parse_account_metrics(data)
        if metrics is None:
            result.status = CollectionStatus.FAILED
            result.error = "X returned no public_metrics.followers_count."
            return await self._finish(run, result)

        # Truncated to the hour so retries within the same hour are idempotent
        # against the unique constraint rather than producing near-duplicates.
        captured_at = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

        existing = await self.db.scalar(
            select(AccountMetricSnapshot).where(
                AccountMetricSnapshot.x_account_id == self.account.id,
                AccountMetricSnapshot.captured_at == captured_at,
            )
        )
        if existing is None:
            self.db.add(
                AccountMetricSnapshot(
                    x_account_id=self.account.id,
                    captured_at=captured_at,
                    provenance=Provenance.MEASURED,
                    **metrics,
                )
            )
            result.snapshots_written = 1
            await self.db.flush()

        result.status = CollectionStatus.SUCCEEDED
        result.details = {"followers_count": metrics["followers_count"]}
        return await self._finish(run, result)

    # ----------------------------------------------------- post discovery
    async def discover_posts(self, max_pages: int = 3) -> CollectionRun:
        """Find posts we have not recorded yet.

        Uses `since_id` to page only over what is new. On the first run there is
        no watermark, so it walks back a few pages — capturing what history it
        still can, while marking anything already past the window as having
        permanently unavailable impressions.
        """
        run = await self._begin(CollectionKind.POST_DISCOVERY)
        result = CollectionResult(CollectionKind.POST_DISCOVERY, CollectionStatus.RUNNING)

        newest = await self.db.scalar(
            select(func.max(Post.x_post_id)).where(Post.x_account_id == self.account.id)
        )

        pagination_token: str | None = None
        pages = 0

        try:
            while pages < max_pages:
                response = await self.client.get_own_posts(
                    max_results=PAGE_SIZE,
                    pagination_token=pagination_token,
                    since_id=newest if newest and pages == 0 else None,
                    include_private_metrics=True,
                )
                pages += 1

                created = await self._upsert_posts(response.items)
                result.posts_discovered += created

                pagination_token = response.next_token
                if not pagination_token or not response.items:
                    break
                # Once a watermark exists, one page of new posts is plenty; we
                # are topping up, not backfilling.
                if newest:
                    break

        except (XBudgetExceededError, XRateLimitError, XCapabilityUnavailableError) as exc:
            result.status = (
                CollectionStatus.PARTIAL if result.posts_discovered else CollectionStatus.SKIPPED
            )
            result.error = str(exc)
            result.degraded_reason = type(exc).__name__
            return await self._finish(run, result)
        except XApiError as exc:
            result.status = CollectionStatus.FAILED
            result.error = str(exc)
            return await self._finish(run, result)

        result.status = CollectionStatus.SUCCEEDED
        result.details = {"pages_fetched": pages, "had_watermark": bool(newest)}
        return await self._finish(run, result)

    async def _upsert_posts(self, items: list[dict[str, object]]) -> int:
        """Insert posts we have not seen. Existing posts are left untouched.

        Post rows are immutable facts about publication; everything that changes
        over time lives in the snapshot table.
        """
        created = 0
        now = datetime.now(UTC)

        for payload in items:
            parsed = parse_post(payload)
            if parsed is None:
                continue

            exists = await self.db.scalar(
                select(Post.id).where(
                    Post.x_account_id == self.account.id,
                    Post.x_post_id == parsed["x_post_id"],
                )
            )
            if exists is not None:
                continue

            posted_at = parsed["posted_at"]
            closes_at = schedule.window_closes_at(posted_at)
            already_closed = closes_at <= now

            post = Post(
                x_account_id=self.account.id,
                first_seen_at=now,
                metrics_window_closes_at=closes_at,
                # Recorded at discovery so the dashboard can say "impressions
                # were never available for this post" rather than showing a gap
                # that looks like a collection failure.
                discovered_after_window_closed=already_closed,
                **parsed,
            )
            self.db.add(post)
            await self.db.flush()
            created += 1

            # Capture metrics immediately from the payload we already paid for.
            metrics = parse_metrics(payload)
            await self._write_snapshot(post, metrics, now, is_final_freeze=False)

        return created

    # ------------------------------------------------------ post metrics
    async def collect_post_metrics(self) -> CollectionRun:
        """Snapshot every post that is due, most urgent first."""
        budget_state = await self._budget_state()
        due = await self._posts_due(budget_state)

        kind = (
            CollectionKind.FINAL_FREEZE
            if any(d.is_final_freeze for _, d in due)
            else CollectionKind.POST_METRICS
        )
        run = await self._begin(kind)
        result = CollectionResult(kind, CollectionStatus.RUNNING)

        if budget_state is not schedule.BudgetState.HEALTHY:
            result.degraded_reason = f"budget_{budget_state.value}"

        if not due:
            result.status = CollectionStatus.SKIPPED
            result.details = {"reason": "nothing due", "budget_state": budget_state.value}
            return await self._finish(run, result)

        # One request covers many posts, so fetch the timeline once and match
        # against it rather than paying per post.
        try:
            response = await self.client.get_own_posts(
                max_results=PAGE_SIZE, include_private_metrics=True
            )
        except (XBudgetExceededError, XRateLimitError) as exc:
            result.status = CollectionStatus.SKIPPED
            result.error = str(exc)
            result.skipped_count = len(due)
            result.degraded_reason = type(exc).__name__
            # A skipped freeze is data loss, so say so loudly rather than
            # burying it in a status field.
            if kind is CollectionKind.FINAL_FREEZE:
                log.error(
                    "collection.freeze_skipped",
                    account=self.account.username,
                    posts_at_risk=sum(1 for _, d in due if d.is_final_freeze),
                    reason=str(exc),
                )
            return await self._finish(run, result)
        except XApiError as exc:
            result.status = CollectionStatus.FAILED
            result.error = str(exc)
            return await self._finish(run, result)

        by_x_id = {str(item.get("id")): item for item in response.items if isinstance(item, dict)}
        now = datetime.now(UTC)

        for post, decision in due[:MAX_POSTS_PER_CYCLE]:
            payload = by_x_id.get(post.x_post_id)
            if payload is None:
                # Not on the current page — usually because it has scrolled past
                # 100 posts. Older posts fall off first, which is why the freeze
                # window opens a full day early.
                result.skipped_count += 1
                continue

            metrics = parse_metrics(payload)
            await self._write_snapshot(post, metrics, now, is_final_freeze=decision.is_final_freeze)
            result.snapshots_written += 1

        result.skipped_count += max(0, len(due) - MAX_POSTS_PER_CYCLE)
        result.status = (
            CollectionStatus.PARTIAL if result.skipped_count else CollectionStatus.SUCCEEDED
        )
        result.details = {
            "due": len(due),
            "budget_state": budget_state.value,
            "freezes": sum(1 for _, d in due if d.is_final_freeze),
        }
        return await self._finish(run, result)

    async def _posts_due(
        self, budget_state: schedule.BudgetState
    ) -> list[tuple[Post, schedule.SnapshotDecision]]:
        now = datetime.now(UTC)

        # Only posts still inside the window can yield anything new.
        candidates = list(
            await self.db.scalars(
                select(Post)
                .where(
                    Post.x_account_id == self.account.id,
                    Post.metrics_window_closes_at > now,
                    Post.final_freeze_at.is_(None),
                )
                .order_by(Post.posted_at.desc())
            )
        )

        decisions: list[tuple[Post, schedule.SnapshotDecision]] = []
        for post in candidates:
            last = await self.db.scalar(
                select(func.max(PostMetricSnapshot.captured_at)).where(
                    PostMetricSnapshot.post_id == post.id
                )
            )
            decision = schedule.decide(
                posted_at=post.posted_at,
                last_snapshot_at=last,
                has_final_freeze=post.final_freeze_at is not None,
                impressions_ever_collected=post.impressions_ever_collected,
                now=now,
                budget_state=budget_state,
            )
            if decision.due:
                decisions.append((post, decision))

        decisions.sort(key=lambda item: item[1].priority)
        return decisions

    async def _write_snapshot(
        self,
        post: Post,
        metrics: dict[str, object],
        captured_at: datetime,
        *,
        is_final_freeze: bool,
    ) -> PostMetricSnapshot | None:
        # Minute precision keeps retries within the same minute idempotent.
        captured_at = captured_at.replace(second=0, microsecond=0)

        existing = await self.db.scalar(
            select(PostMetricSnapshot).where(
                PostMetricSnapshot.post_id == post.id,
                PostMetricSnapshot.captured_at == captured_at,
            )
        )
        if existing is not None:
            # A snapshot already exists for this minute — but if *this* call is
            # the final freeze, the bookkeeping still has to happen. Returning
            # early here would leave `final_freeze_at` unset, so the post would
            # be re-selected as freeze-due on every subsequent cycle, paying for
            # reads over and over until it fell off the cliff unmarked.
            if is_final_freeze and not existing.is_final_freeze:
                existing.is_final_freeze = True
                post.final_freeze_at = captured_at
                await self.db.flush()
            return existing

        snapshot = PostMetricSnapshot(
            post_id=post.id,
            captured_at=captured_at,
            post_age_hours=schedule.age_hours(post.posted_at, captured_at),
            provenance=Provenance.MEASURED,
            is_final_freeze=is_final_freeze,
            **metrics,
        )
        self.db.add(snapshot)

        if metrics.get("impression_count") is not None:
            post.impressions_ever_collected = True
        if is_final_freeze:
            post.final_freeze_at = captured_at

        await self.db.flush()
        return snapshot


async def build_collector(db: AsyncSession, account: XAccount) -> Collector:
    """Wire a collector with an authenticated client for one account."""
    from app.services.x_account_service import XAccountService

    service = XAccountService(db)
    scopes = await service.granted_scopes(account.id)
    client = XApiClient(
        db,
        account,
        token_provider=lambda: service.get_valid_access_token(account),
        granted_scopes=scopes,
    )
    return Collector(db, client)


async def active_account_ids(db: AsyncSession) -> list[uuid.UUID]:
    return list(
        await db.scalars(
            select(XAccount.id).where(
                XAccount.is_active.is_(True), XAccount.connected_at.isnot(None)
            )
        )
    )
