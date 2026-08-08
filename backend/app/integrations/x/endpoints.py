"""Registry of the X API endpoints this application is allowed to call.

The brief says "never invent API endpoints". Rather than relying on care, this
registry makes it structural: `XApiClient` refuses to issue a request for any
endpoint not declared here, so an invented path fails immediately and locally
instead of producing a confusing 404 from X.

Each entry also declares what the call needs and costs, which is what lets the
client check scopes, capabilities and budget *before* spending money.

Everything here was cross-checked against X's documentation in August 2026.
Where a detail could not be confirmed from primary sources (the docs host was
unreachable), it is flagged and the capability probe determines the truth at
runtime rather than the code assuming it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.models.enums import XCapability


class CostClass(enum.StrEnum):
    """How a request is billed under pay-per-use.

    OWNED_READ is the rate that makes this project viable: since April 2026,
    requests by your own app for your own data bill at $0.001/resource rather
    than $0.005. Nearly everything this system does qualifies.
    """

    OWNED_READ = "OWNED_READ"
    GENERAL_READ = "GENERAL_READ"
    FREE = "FREE"  # OAuth token operations are not metered reads


class HttpMethod(enum.StrEnum):
    GET = "GET"
    POST = "POST"


@dataclass(frozen=True)
class Endpoint:
    key: str
    method: HttpMethod
    path: str
    cost_class: CostClass
    required_scopes: tuple[str, ...] = ()
    capability: XCapability | None = None
    # Resources billed per request when the response carries no countable
    # objects; otherwise the client counts what came back.
    default_resource_count: int = 1
    notes: str = ""
    verified: bool = True

    def url(self, base: str, **params: str) -> str:
        return f"{base.rstrip('/')}{self.path.format(**params)}"


# --- Scopes ----------------------------------------------------------------
# Only read scopes are requested by default. `tweet.write` is appended solely
# when X_ENABLE_WRITE_ACTIONS is true (decision 4), so with the flag off the
# agent is not merely forbidden from posting — it holds no token that could.
BASE_SCOPES: tuple[str, ...] = (
    "tweet.read",
    "users.read",
    # Without offline.access X issues no refresh token, access tokens expire in
    # two hours, and unattended collection becomes impossible.
    "offline.access",
)

OPTIONAL_READ_SCOPES: tuple[str, ...] = (
    "like.read",
    "bookmark.read",
    "follows.read",
)

WRITE_SCOPES: tuple[str, ...] = ("tweet.write",)


def requested_scopes(enable_write: bool) -> tuple[str, ...]:
    scopes = BASE_SCOPES + OPTIONAL_READ_SCOPES
    return scopes + WRITE_SCOPES if enable_write else scopes


# --- Endpoints -------------------------------------------------------------

ME = Endpoint(
    key="users.me",
    method=HttpMethod.GET,
    path="/2/users/me",
    cost_class=CostClass.OWNED_READ,
    required_scopes=("users.read", "tweet.read"),
    capability=XCapability.READ_OWN_PROFILE,
    notes="Profile plus public_metrics.followers_count. The only follower "
    "figure X exposes — there is no history endpoint, so we snapshot it.",
)

USER_TWEETS = Endpoint(
    key="users.tweets",
    method=HttpMethod.GET,
    path="/2/users/{user_id}/tweets",
    cost_class=CostClass.OWNED_READ,
    required_scopes=("tweet.read", "users.read"),
    capability=XCapability.READ_OWN_POSTS,
    notes="Up to 100 posts per page. Billing is per resource returned, so "
    "paging larger saves rate limit but not money.",
)

USER_FOLLOWERS = Endpoint(
    key="users.followers",
    method=HttpMethod.GET,
    path="/2/users/{user_id}/followers",
    cost_class=CostClass.OWNED_READ,
    required_scopes=("follows.read", "users.read"),
    capability=XCapability.READ_FOLLOWERS_LIST,
    verified=False,
    notes="UNCERTAIN. Removed from Basic/Pro tiers at one point, yet listed "
    "among Owned Reads in the April 2026 pricing update. Sources conflict, so "
    "availability is decided by the probe, never assumed.",
)

USER_LIKED = Endpoint(
    key="users.liked_tweets",
    method=HttpMethod.GET,
    path="/2/users/{user_id}/liked_tweets",
    cost_class=CostClass.OWNED_READ,
    required_scopes=("like.read", "tweet.read"),
    capability=XCapability.READ_LIKED_POSTS,
)

USER_BOOKMARKS = Endpoint(
    key="users.bookmarks",
    method=HttpMethod.GET,
    path="/2/users/{user_id}/bookmarks",
    cost_class=CostClass.OWNED_READ,
    required_scopes=("bookmark.read", "tweet.read"),
    capability=XCapability.READ_BOOKMARKS,
    notes="A developer report suggests this endpoint was billed at the general "
    "read rate despite qualifying as an Owned Read. The ledger records the "
    "class we expect; reconcile against your invoice.",
)

REGISTRY: dict[str, Endpoint] = {
    e.key: e for e in (ME, USER_TWEETS, USER_FOLLOWERS, USER_LIKED, USER_BOOKMARKS)
}


def get_endpoint(key: str) -> Endpoint:
    try:
        return REGISTRY[key]
    except KeyError:
        raise ValueError(
            f"Unknown X endpoint {key!r}. Endpoints must be declared in "
            f"app/integrations/x/endpoints.py before they can be called. "
            f"Known: {sorted(REGISTRY)}"
        ) from None


# --- Field selections ------------------------------------------------------
# Requesting a field X will not return for a given post is harmless (it is
# simply absent), which is what lets one field list serve accounts with
# different access levels.

USER_FIELDS = (
    "id",
    "name",
    "username",
    "created_at",
    "description",
    "profile_image_url",
    "public_metrics",
    "verified",
)

PUBLIC_TWEET_FIELDS = (
    "id",
    "text",
    "created_at",
    "conversation_id",
    "in_reply_to_user_id",
    "referenced_tweets",
    "attachments",
    "entities",
    "lang",
    "public_metrics",
)

# Own posts under 30 days old only, and only under OAuth 2.0 user context.
# Past that window these disappear from the API permanently — which is why the
# collector's day-29 snapshot exists.
PRIVATE_TWEET_FIELDS = (
    "non_public_metrics",
    "organic_metrics",
)

METRICS_WINDOW_DAYS = 30
