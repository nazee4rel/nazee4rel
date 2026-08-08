"""Topic classification: the one part of the analytics engine that needs a model.

Formats (media, links, threads, length) are detected mechanically at collection
time and carry no classification error. Subject matter is not like that — you
cannot regex your way to "this post was about hiring" — so this is where a
language model earns its place.

The taxonomy is closed, and that is the important design decision. Free-form
labels drift between runs: this week's "AI tooling" is next week's "developer
tools", and the moment that happens, comparing topic performance across periods
stops meaning anything — which is the only reason to categorise posts at all. So
the model may assign from the list and may *suggest* additions, but adding one
is a T1 action the owner approves.

Assignments are always `INFERRED` and always record which model made them, so a
change of model or taxonomy is traceable rather than silently rewriting history.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.client import AgentModelError, ReasoningClient
from app.agent.prompts import SYSTEM_CLASSIFIER, new_nonce, render_classification_prompt
from app.agent.schemas import TopicClassificationOutput
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.content import Post
from app.models.enums import Provenance
from app.models.revenue import PostTopic, Topic

log = get_logger(__name__)

# A starting taxonomy so the first run has something to work with. Broad and
# deliberately generic — these are meant to be edited into something that fits
# the account, not treated as the right answer.
DEFAULT_TOPICS: tuple[tuple[str, str], ...] = (
    ("Product & building", "Shipping, features, engineering and product decisions."),
    ("Industry commentary", "Opinion or analysis about the wider field or market."),
    ("Personal update", "News about the author: milestones, moves, reflections."),
    ("Educational", "Explanations, how-tos, threads that teach something."),
    ("Promotion", "Announcing, selling or linking to something of the author's."),
    ("Engagement", "Questions, polls and prompts aimed at starting conversation."),
    ("Commentary on others", "Replies, quotes and responses to other people's posts."),
    ("Announcement", "Launches, events, releases and dated news."),
)

# One batch per run. Classification is cheap per post but not free, and the
# unclassified backlog drains over a few cycles rather than in one expensive
# call.
MAX_POSTS_PER_RUN = 40
POST_TEXT_LIMIT = 400
# Below this the model is guessing rather than classifying, and a wrong label
# is worse than none because it silently pollutes cross-period comparison.
MIN_CONFIDENCE = 0.35


@dataclass
class ClassificationResult:
    posts_considered: int = 0
    assignments_written: int = 0
    unknown_topics_ignored: list[str] = field(default_factory=list)
    low_confidence_skipped: int = 0
    suggested_topics: list[str] = field(default_factory=list)
    note: str = ""


class TopicClassifier:
    def __init__(self, db: AsyncSession, client: ReasoningClient) -> None:
        self.db = db
        self.client = client
        self.settings = get_settings()

    async def ensure_taxonomy(self, x_account_id: uuid.UUID) -> list[Topic]:
        """Seed the default taxonomy the first time, then leave it alone."""
        existing = list(
            await self.db.scalars(
                select(Topic).where(Topic.x_account_id == x_account_id, Topic.is_active.is_(True))
            )
        )
        if existing:
            return existing

        seeded = [
            Topic(x_account_id=x_account_id, name=name, description=description)
            for name, description in DEFAULT_TOPICS
        ]
        self.db.add_all(seeded)
        await self.db.flush()
        log.info("agent.taxonomy_seeded", account_id=str(x_account_id), count=len(seeded))
        return seeded

    async def classify_pending(
        self, x_account_id: uuid.UUID, *, days: int = 90
    ) -> ClassificationResult:
        result = ClassificationResult()
        taxonomy = await self.ensure_taxonomy(x_account_id)
        by_name = {topic.name.casefold(): topic for topic in taxonomy}

        since = datetime.now(UTC) - timedelta(days=days)
        already_classified = select(PostTopic.post_id).where(PostTopic.post_id == Post.id).exists()
        posts = list(
            await self.db.scalars(
                select(Post)
                .where(
                    Post.x_account_id == x_account_id,
                    Post.posted_at >= since,
                    func.length(Post.text) > 0,
                    ~already_classified,
                )
                .order_by(Post.posted_at.desc())
                .limit(MAX_POSTS_PER_RUN)
            )
        )
        result.posts_considered = len(posts)
        if not posts:
            result.note = "Every post in the window already carries a topic."
            return result

        refs = {f"c{index}": post for index, post in enumerate(posts, start=1)}
        nonce = new_nonce()
        prompt = render_classification_prompt(
            [(topic.name, topic.description) for topic in taxonomy],
            [(ref, post.text[:POST_TEXT_LIMIT]) for ref, post in refs.items()],
            nonce=nonce,
        )

        try:
            response = await self.client.ask(
                system=SYSTEM_CLASSIFIER,
                user_content=prompt,
                output_format=TopicClassificationOutput,
                model=self.settings.anthropic_model_fast,
                max_tokens=4000,
            )
        except AgentModelError as exc:
            result.note = f"Classification did not run: {exc}"
            log.warning("agent.classification_unavailable", error=str(exc))
            return result

        classified_by = f"{response.model}/taxonomy-v1"
        for assignment in response.parsed.assignments:
            post = refs.get(assignment.post_ref)
            if post is None:
                continue
            if assignment.confidence < MIN_CONFIDENCE:
                result.low_confidence_skipped += 1
                continue
            for name in assignment.topic_names:
                topic = by_name.get(name.strip().casefold())
                if topic is None:
                    # The taxonomy is closed. A name outside it is dropped, not
                    # created — creating it here is exactly the drift this
                    # design exists to prevent.
                    result.unknown_topics_ignored.append(name)
                    continue
                self.db.add(
                    PostTopic(
                        post_id=post.id,
                        topic_id=topic.id,
                        confidence=round(assignment.confidence, 3),
                        classified_by=classified_by[:80],
                        provenance=Provenance.INFERRED,
                    )
                )
                result.assignments_written += 1

        result.suggested_topics = [
            name
            for name in response.parsed.suggested_new_topics
            if name.strip().casefold() not in by_name
        ]
        await self.db.flush()
        log.info(
            "agent.classified",
            posts=result.posts_considered,
            assignments=result.assignments_written,
            ignored=len(result.unknown_topics_ignored),
        )
        return result
