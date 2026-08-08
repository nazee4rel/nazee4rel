"""The capability probe.

Section 1.5 of the architecture doc: X's access rules change, are documented
inconsistently, and differ by billing mode. Rather than hardcoding what the API
can do, the system asks it — once at connect time, weekly thereafter — and
drives both the collector and the dashboard from the recorded answers.

Probing costs money (a read is a read), so each probe requests the smallest
possible page. The whole sweep is a handful of resources.

The interesting case is `READ_NON_PUBLIC_METRICS`. A 200 response does not mean
impressions are available: X returns 200 while simply omitting those fields for
posts older than 30 days, or when the access level does not include them. So
this probe inspects the payload rather than the status code — and reports
INCONCLUSIVE, recorded as UNKNOWN, when the account has no post recent enough to
decide. Claiming "unavailable" there would be wrong, and claiming "available"
would be a guess.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.integrations.x import endpoints as ep
from app.integrations.x.client import XApiClient
from app.integrations.x.errors import (
    XApiError,
    XAuthError,
    XBudgetExceededError,
    XForbiddenError,
    XNotFoundError,
    XRateLimitError,
)
from app.models.enums import AuditAction, CapabilityStatus, XCapability
from app.models.x_account import AccountCapability, XAccount
from app.services.audit_service import AuditService

log = get_logger(__name__)


@dataclass
class ProbeResult:
    capability: XCapability
    status: CapabilityStatus
    detail: str = ""
    evidence: dict[str, object] | None = None


class CapabilityProbe:
    def __init__(self, db: AsyncSession, client: XApiClient) -> None:
        self.db = db
        self.client = client
        self.audit = AuditService(db)

    async def run(self) -> list[ProbeResult]:
        account = self.client.account
        results: list[ProbeResult] = []

        # Ordered so cheap, foundational checks run first: if the profile read
        # fails there is no point probing anything else.
        profile = await self._probe_profile()
        results.append(profile)

        if profile.status is not CapabilityStatus.AVAILABLE:
            for cap in XCapability:
                if cap is not XCapability.READ_OWN_PROFILE:
                    results.append(
                        ProbeResult(
                            cap,
                            CapabilityStatus.UNKNOWN,
                            "Not probed: the profile read failed, so nothing else could be tested.",
                        )
                    )
            await self._persist(account.id, results)
            return results

        results.extend(await self._probe_posts_and_metrics())
        results.append(await self._probe_simple(ep.USER_FOLLOWERS, XCapability.READ_FOLLOWERS_LIST))
        results.append(await self._probe_simple(ep.USER_LIKED, XCapability.READ_LIKED_POSTS))
        results.append(await self._probe_simple(ep.USER_BOOKMARKS, XCapability.READ_BOOKMARKS))
        results.append(self._probe_write())

        await self._persist(account.id, results)
        await self.audit.record(
            AuditAction.CAPABILITY_PROBE_RUN,
            user_id=account.user_id,
            x_account_id=account.id,
            available=[
                r.capability.value for r in results if r.status is CapabilityStatus.AVAILABLE
            ],
        )
        return results

    # ------------------------------------------------------------- probes
    async def _probe_profile(self) -> ProbeResult:
        try:
            response = await self.client.request(
                ep.ME.key,
                params={"user.fields": "id,username,public_metrics"},
                expected_resources=1,
                bypass_capability_check=True,
            )
        except XApiError as exc:
            return self._classify(XCapability.READ_OWN_PROFILE, exc)

        metrics = (response.data or {}).get("public_metrics") if response.data else None
        if not metrics or "followers_count" not in metrics:
            return ProbeResult(
                XCapability.READ_OWN_PROFILE,
                CapabilityStatus.UNAVAILABLE,
                "Profile readable but public_metrics.followers_count was absent, so "
                "follower tracking is not possible.",
            )

        return ProbeResult(
            XCapability.READ_OWN_PROFILE,
            CapabilityStatus.AVAILABLE,
            "Profile and follower count readable.",
            {"followers_count": metrics.get("followers_count")},
        )

    async def _probe_posts_and_metrics(self) -> list[ProbeResult]:
        """One request settles three capabilities, so we pay for it once."""
        try:
            response = await self.client.get_own_posts(max_results=5, include_private_metrics=True)
        except XApiError as exc:
            return [
                self._classify(XCapability.READ_OWN_POSTS, exc),
                self._classify(XCapability.READ_PUBLIC_METRICS, exc),
                self._classify(XCapability.READ_NON_PUBLIC_METRICS, exc),
                self._classify(XCapability.READ_ORGANIC_METRICS, exc),
            ]

        posts = response.items
        results = [
            ProbeResult(
                XCapability.READ_OWN_POSTS,
                CapabilityStatus.AVAILABLE,
                f"Timeline readable ({len(posts)} recent posts sampled).",
            )
        ]

        if not posts:
            # An empty timeline is not evidence about field availability.
            for cap in (
                XCapability.READ_PUBLIC_METRICS,
                XCapability.READ_NON_PUBLIC_METRICS,
                XCapability.READ_ORGANIC_METRICS,
            ):
                results.append(
                    ProbeResult(
                        cap,
                        CapabilityStatus.UNKNOWN,
                        "Inconclusive: the account has no posts to inspect. "
                        "This will resolve once you post.",
                    )
                )
            return results

        results.append(
            self._field_result(
                XCapability.READ_PUBLIC_METRICS,
                posts,
                "public_metrics",
                "Likes, replies, reposts and bookmarks are readable.",
            )
        )

        # Impressions live here, and the 30-day window is why this matters most.
        recent = [p for p in posts if _within_metrics_window(p.get("created_at"))]
        if not recent:
            note = (
                f"Inconclusive: every sampled post is older than "
                f"{ep.METRICS_WINDOW_DAYS} days, and X omits these fields beyond "
                f"that window regardless of access level. Post something recent "
                f"and re-run the probe."
            )
            results.append(
                ProbeResult(XCapability.READ_NON_PUBLIC_METRICS, CapabilityStatus.UNKNOWN, note)
            )
            results.append(
                ProbeResult(XCapability.READ_ORGANIC_METRICS, CapabilityStatus.UNKNOWN, note)
            )
            return results

        results.append(
            self._field_result(
                XCapability.READ_NON_PUBLIC_METRICS,
                recent,
                "non_public_metrics",
                "Impressions, link clicks and profile clicks are readable for "
                "posts under 30 days old.",
                unavailable_note=(
                    "Recent posts were returned without non_public_metrics. Either "
                    "the access level excludes them or the required scope was not "
                    "granted. Impressions cannot be collected."
                ),
            )
        )
        results.append(
            self._field_result(
                XCapability.READ_ORGANIC_METRICS,
                recent,
                "organic_metrics",
                "Organic engagement breakdown is readable for recent posts.",
                unavailable_note="Recent posts were returned without organic_metrics.",
            )
        )
        return results

    async def _probe_simple(self, endpoint: ep.Endpoint, capability: XCapability) -> ProbeResult:
        try:
            await self.client.request(
                endpoint.key,
                path_params={"user_id": self.client.account.x_user_id},
                params={"max_results": 1},
                expected_resources=1,
                bypass_capability_check=True,
            )
        except XApiError as exc:
            return self._classify(capability, exc)
        return ProbeResult(capability, CapabilityStatus.AVAILABLE, "Endpoint reachable.")

    def _probe_write(self) -> ProbeResult:
        """Write capability is decided locally, never by attempting a post.

        Probing this by posting would be an irreversible side effect, which the
        brief forbids. The scope grant is sufficient evidence.
        """
        if not self.client.settings.x_enable_write_actions:
            return ProbeResult(
                XCapability.WRITE_POSTS,
                CapabilityStatus.UNAVAILABLE,
                "Disabled by configuration (X_ENABLE_WRITE_ACTIONS=false). The "
                "tweet.write scope was not requested, so publishing is impossible.",
            )
        if "tweet.write" not in self.client.granted_scopes:
            return ProbeResult(
                XCapability.WRITE_POSTS,
                CapabilityStatus.FORBIDDEN,
                "Write actions are enabled but the tweet.write scope was not "
                "granted. Reconnect the account and approve it.",
            )
        return ProbeResult(
            XCapability.WRITE_POSTS,
            CapabilityStatus.AVAILABLE,
            "Drafting is possible. Nothing publishes without your per-draft approval.",
        )

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _field_result(
        capability: XCapability,
        posts: list[dict[str, object]],
        field: str,
        available_note: str,
        unavailable_note: str | None = None,
    ) -> ProbeResult:
        present = sum(1 for p in posts if isinstance(p.get(field), dict))
        if present:
            return ProbeResult(
                capability,
                CapabilityStatus.AVAILABLE,
                available_note,
                {"posts_sampled": len(posts), "posts_with_field": present},
            )
        return ProbeResult(
            capability,
            CapabilityStatus.UNAVAILABLE,
            unavailable_note or f"{field} was absent from every sampled post.",
            {"posts_sampled": len(posts), "posts_with_field": 0},
        )

    @staticmethod
    def _classify(capability: XCapability, exc: XApiError) -> ProbeResult:
        """Map a failure onto a capability status.

        The distinction that matters: FORBIDDEN and UNAVAILABLE are settled
        answers, while ERROR and UNKNOWN mean "ask again later". Recording a
        transient failure as a settled absence would permanently disable a
        feature the account actually has.
        """
        if isinstance(exc, XForbiddenError):
            return ProbeResult(
                capability,
                CapabilityStatus.FORBIDDEN,
                "X returned 403 — this access level or scope set does not permit it.",
            )
        if isinstance(exc, XNotFoundError):
            return ProbeResult(
                capability, CapabilityStatus.UNAVAILABLE, "X returned 404 for this endpoint."
            )
        if isinstance(exc, XAuthError):
            return ProbeResult(
                capability, CapabilityStatus.ERROR, "Authentication failed; reconnect the account."
            )
        if isinstance(exc, XRateLimitError):
            return ProbeResult(
                capability, CapabilityStatus.UNKNOWN, "Rate limited during the probe; retry later."
            )
        if isinstance(exc, XBudgetExceededError):
            return ProbeResult(
                capability,
                CapabilityStatus.UNKNOWN,
                "Skipped: probing would exceed the configured API budget.",
            )
        return ProbeResult(capability, CapabilityStatus.ERROR, str(exc)[:400])

    async def _persist(self, account_id: uuid.UUID, results: list[ProbeResult]) -> None:
        now = datetime.now(UTC)
        existing = {
            row.capability: row
            for row in await self.db.scalars(
                select(AccountCapability).where(AccountCapability.x_account_id == account_id)
            )
        }

        for result in results:
            row = existing.get(result.capability)
            if row is None:
                row = AccountCapability(x_account_id=account_id, capability=result.capability)
                self.db.add(row)

            # An inconclusive probe must not overwrite a previously settled
            # answer with "don't know".
            if result.status is CapabilityStatus.UNKNOWN and row.status in (
                CapabilityStatus.AVAILABLE,
                CapabilityStatus.UNAVAILABLE,
                CapabilityStatus.FORBIDDEN,
            ):
                row.last_checked_at = now
                row.details = {**(row.details or {}), "last_inconclusive": result.detail}
                continue

            row.status = result.status
            row.last_checked_at = now
            row.last_error = (
                result.detail
                if result.status in (CapabilityStatus.ERROR, CapabilityStatus.FORBIDDEN)
                else None
            )
            row.details = {"detail": result.detail, **(result.evidence or {})}
            if result.status is CapabilityStatus.AVAILABLE:
                row.last_available_at = now

        await self.db.flush()


async def probe_account(db: AsyncSession, account: XAccount) -> list[ProbeResult]:
    """Convenience entrypoint used by the API and, from Phase 4, the scheduler."""
    from app.services.x_account_service import XAccountService

    service = XAccountService(db)
    scopes = await service.granted_scopes(account.id)

    async with XApiClient(
        db,
        account,
        token_provider=lambda: service.get_valid_access_token(account),
        granted_scopes=scopes,
    ) as client:
        return await CapabilityProbe(db, client).run()


def _within_metrics_window(created_at: object) -> bool:
    """True when a post is recent enough for non-public metrics to exist."""
    if not isinstance(created_at, str):
        return False
    try:
        posted = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    age_days = (datetime.now(UTC) - posted).days
    return age_days < ep.METRICS_WINDOW_DAYS
