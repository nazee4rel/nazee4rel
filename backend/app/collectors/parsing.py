"""Turn X API payloads into our own rows.

Kept separate from the collectors so parsing can be tested against captured
payloads without any network or database involved.

The recurring rule here: a field X did not return becomes `None`, never `0`.
A missing impression count and an impression count of zero are different facts,
and once the 30-day window closes the difference can never be re-derived.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models.content import PostType


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _int_or_none(container: Any, key: str) -> int | None:
    """Read an integer, preserving the distinction between absent and zero."""
    if not isinstance(container, dict):
        return None
    value = container.get(key)
    return int(value) if isinstance(value, (int, float)) else None


def _int_or_zero(container: Any, key: str) -> int:
    """For public metrics, which X always returns when the field is requested."""
    return _int_or_none(container, key) or 0


def classify_post_type(payload: dict[str, Any]) -> PostType:
    referenced = payload.get("referenced_tweets")
    if isinstance(referenced, list):
        kinds = {r.get("type") for r in referenced if isinstance(r, dict)}
        # Order matters: a quote-reply is more usefully counted as a reply.
        if "replied_to" in kinds:
            return PostType.REPLY
        if "quoted" in kinds:
            return PostType.QUOTE
        if "retweeted" in kinds:
            return PostType.REPOST
    return PostType.ORIGINAL


def extract_features(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic format features used by the Phase 5 analytics."""
    entities = payload.get("entities") if isinstance(payload.get("entities"), dict) else {}
    attachments = payload.get("attachments") if isinstance(payload.get("attachments"), dict) else {}
    raw_text = payload.get("text")
    text: str = raw_text if isinstance(raw_text, str) else ""

    urls = entities.get("urls") if isinstance(entities, dict) else None
    media_keys = attachments.get("media_keys") if isinstance(attachments, dict) else None

    return {
        "has_link": bool(urls),
        "has_media": bool(media_keys),
        "has_poll": bool(attachments.get("poll_ids")) if isinstance(attachments, dict) else False,
        "char_count": len(text),
        "lang": payload.get("lang") if isinstance(payload.get("lang"), str) else None,
    }


def parse_post(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Map one X post object onto Post column values."""
    post_id = payload.get("id")
    posted_at = parse_timestamp(payload.get("created_at"))
    if not isinstance(post_id, str) or posted_at is None:
        # Without an id or a timestamp the row is useless — the whole schedule
        # is computed from post age.
        return None

    features = extract_features(payload)
    conversation_id = payload.get("conversation_id")

    return {
        "x_post_id": post_id,
        "text": payload.get("text") if isinstance(payload.get("text"), str) else "",
        "posted_at": posted_at,
        "post_type": classify_post_type(payload),
        "conversation_id": conversation_id if isinstance(conversation_id, str) else None,
        "in_reply_to_user_id": (
            payload.get("in_reply_to_user_id")
            if isinstance(payload.get("in_reply_to_user_id"), str)
            else None
        ),
        # A post whose conversation is its own id starts a thread; anything else
        # continues one.
        "is_thread": bool(conversation_id) and conversation_id != post_id,
        "raw_payload": payload,
        **features,
    }


def parse_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    """Map one X post object onto PostMetricSnapshot column values.

    `non_public_available` records whether X actually returned the private
    block, so a later reader can distinguish a genuine absence (post too old,
    or access level lacks it) from a collector bug.
    """
    public = payload.get("public_metrics")
    non_public = payload.get("non_public_metrics")
    organic = payload.get("organic_metrics")

    return {
        # Public metrics: X returns these whenever the field is requested.
        "like_count": _int_or_zero(public, "like_count"),
        "reply_count": _int_or_zero(public, "reply_count"),
        "retweet_count": _int_or_zero(public, "retweet_count"),
        "quote_count": _int_or_zero(public, "quote_count"),
        "bookmark_count": _int_or_zero(public, "bookmark_count"),
        "public_impression_count": _int_or_none(public, "impression_count"),
        # Non-public: NULL when unavailable. Never coerced to zero.
        "impression_count": _int_or_none(non_public, "impression_count"),
        "url_link_clicks": _int_or_none(non_public, "url_link_clicks"),
        "user_profile_clicks": _int_or_none(non_public, "user_profile_clicks"),
        "organic_impression_count": _int_or_none(organic, "impression_count"),
        "organic_like_count": _int_or_none(organic, "like_count"),
        "organic_reply_count": _int_or_none(organic, "reply_count"),
        "organic_retweet_count": _int_or_none(organic, "retweet_count"),
        "non_public_available": isinstance(non_public, dict) and bool(non_public),
    }


def parse_account_metrics(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Map a /users/me response onto AccountMetricSnapshot values."""
    metrics = payload.get("public_metrics")
    if not isinstance(metrics, dict):
        return None
    followers = _int_or_none(metrics, "followers_count")
    if followers is None:
        # Follower count is the entire point of this snapshot.
        return None
    return {
        "followers_count": followers,
        "following_count": _int_or_none(metrics, "following_count"),
        "post_count": _int_or_none(metrics, "tweet_count"),
        "listed_count": _int_or_none(metrics, "listed_count"),
    }
