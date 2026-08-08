"""The agent loop: Observe → Collect → Analyze → Reason → Recommend → Act →
Verify → Report.

This is what makes the system an agent rather than a chat window over a
database. It runs on a schedule with nobody watching, decides for itself whether
there is anything worth reasoning about, spends money only when there is, writes
down what it concluded and why, and — the part that matters — comes back later to
check whether it was right.

Some deliberate properties:

* **It can decide to do nothing.** A run over four data points produces a
  confident, useless answer and charges for it. `has_enough_to_reason` stops
  that, and SKIPPED is a normal outcome, not a failure.
* **It degrades rather than fails.** If the model is unreachable, the run still
  collects, classifies, verifies and reports; it just has no new insights. The
  status is PARTIAL and the reason is recorded.
* **Each stage is attributable.** Timings and notes are stored per stage, so
  "the agent is slow" or "the agent stopped" resolves to a specific stage.
* **Verification is separated from proposal.** This run grades predictions made
  by *earlier* runs; nothing grades itself, and the grades reach the model on
  the next cycle.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.classifier import TopicClassifier
from app.agent.client import AgentModelError, AnthropicClient, ReasoningClient
from app.agent.evidence import Evidence, EvidenceBuilder
from app.agent.executor import ActionExecutor
from app.agent.grounding import check_citations
from app.agent.policy import PolicyViolationError
from app.agent.prompts import SYSTEM_ANALYST, new_nonce, render_analysis_prompt
from app.agent.schemas import (
    PROPOSABLE_ACTIONS,
    AnalysisOutput,
    ProposedRecommendation,
)
from app.agent.verification import VerificationService
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.agent import (
    ActionType,
    AgentRun,
    AgentRunStatus,
    AgentRunTrigger,
    AgentStage,
    Insight,
    Recommendation,
    VerificationResult,
)
from app.models.content import AccountMetricSnapshot
from app.models.enums import Provenance
from app.models.x_account import XAccount
from app.services.cost_service import CostService

log = get_logger(__name__)

# Refresh collection before reasoning if the latest snapshot is older than this.
STALE_SNAPSHOT_HOURS = 2.0
# Reasoning is the expensive call, so it gets the deep model and real effort.
REASONING_EFFORT = "high"


class AgentLoop:
    def __init__(
        self,
        db: AsyncSession,
        account: XAccount,
        client: ReasoningClient | None = None,
    ) -> None:
        self.db = db
        self.account = account
        self.settings = get_settings()
        self.client: ReasoningClient = client or AnthropicClient()
        self.executor = ActionExecutor(db, account)
        self.verification = VerificationService(db)

    async def run(
        self,
        *,
        trigger: AgentRunTrigger = AgentRunTrigger.SCHEDULED,
        days: int = 30,
    ) -> AgentRun:
        run = AgentRun(
            x_account_id=self.account.id,
            trigger=trigger,
            status=AgentRunStatus.RUNNING,
            stage=AgentStage.OBSERVE,
            started_at=datetime.now(UTC),
            stage_log={},
        )
        self.db.add(run)
        await self.db.flush()

        try:
            await self._execute(run, days=days)
        except Exception as exc:  # noqa: BLE001 — a failed cycle must be recorded, not raised
            run.status = AgentRunStatus.FAILED
            run.error = f"{type(exc).__name__}: {exc}"[:2000]
            run.finished_at = datetime.now(UTC)
            await self.db.flush()
            log.exception("agent.run_failed", run_id=str(run.id), stage=run.stage.value)
            return run

        run.finished_at = datetime.now(UTC)
        await self.db.flush()
        return run

    # ------------------------------------------------------------- the cycle
    async def _execute(self, run: AgentRun, *, days: int) -> None:
        # 1. OBSERVE ---------------------------------------------------------
        with StageRecorder(run, AgentStage.OBSERVE) as note:
            budget = await CostService(self.db).budget_status(self.account.id)
            note["budget_state"] = budget["state"]
            note["spent_usd"] = budget["spent_usd"]

        # 2. COLLECT ---------------------------------------------------------
        with StageRecorder(run, AgentStage.COLLECT) as note:
            note.update(await self._refresh_if_stale(budget_state=str(budget["state"])))

        # 3. ANALYZE ---------------------------------------------------------
        with StageRecorder(run, AgentStage.ANALYZE) as note:
            classification = await TopicClassifier(self.db, self.client).classify_pending(
                self.account.id
            )
            note["posts_classified"] = classification.assignments_written
            note["topics_suggested"] = len(classification.suggested_topics)
            if classification.note:
                note["classification_note"] = classification.note
            if classification.unknown_topics_ignored:
                note["unknown_topics_ignored"] = classification.unknown_topics_ignored[:5]

            evidence = await EvidenceBuilder(self.db).build(self.account, days=days)
            run.evidence = evidence.to_json()
            run.ingested_untrusted_content = evidence.ingested_untrusted_content
            note["facts"] = len(evidence.facts)
            note["posts_in_evidence"] = len(evidence.posts)

        if not evidence.has_enough_to_reason():
            run.status = AgentRunStatus.SKIPPED
            run.stage = AgentStage.REPORT
            run.summary = (
                "Not enough history to reason over yet. Reasoning needs at least three "
                "analysed posts or two days of follower snapshots; running it earlier "
                "produces confident advice from noise and charges for the privilege. "
                "Collection is continuing."
            )
            log.info("agent.run_skipped", run_id=str(run.id))
            # Grading still runs: predictions made earlier may now be due.
            await self._verify(run)
            return

        # 4. REASON ----------------------------------------------------------
        analysis: AnalysisOutput | None = None
        with StageRecorder(run, AgentStage.REASON) as note:
            history = await self.verification.history_for_prompt(self.account.id)
            prompt = render_analysis_prompt(evidence, nonce=new_nonce(), verified_history=history)
            note["graded_history_items"] = len(history)
            note["prompt_chars"] = len(prompt)
            try:
                result = await self.client.ask(
                    system=SYSTEM_ANALYST,
                    user_content=prompt,
                    output_format=AnalysisOutput,
                    model=self.settings.anthropic_model_deep,
                    effort=REASONING_EFFORT,
                )
            except AgentModelError as exc:
                # Degraded, not failed: everything deterministic already ran and
                # is worth keeping.
                run.status = AgentRunStatus.PARTIAL
                run.error = str(exc)[:2000]
                note["skipped"] = str(exc)
            else:
                analysis = result.parsed
                run.model = result.model
                run.input_tokens = result.input_tokens
                run.output_tokens = result.output_tokens
                run.cached_input_tokens = result.cached_input_tokens
                note["insights_proposed"] = len(analysis.insights)
                note["recommendations_proposed"] = len(analysis.recommendations)

        # 5. RECOMMEND -------------------------------------------------------
        stored_recommendations: list[tuple[Recommendation, ProposedRecommendation]] = []
        with StageRecorder(run, AgentStage.RECOMMEND) as note:
            if analysis is not None:
                # Assigned before the recommendation pass, which increments the
                # same counter — assigning afterwards would discard its count.
                run.grounding_rejections = await self._store_insights(run, analysis, evidence)
                stored_recommendations = await self._store_recommendations(
                    run, analysis, evidence, days=days
                )
                note["insights_stored"] = run.insights_created
                note["recommendations_stored"] = len(stored_recommendations)
                note["rejected_for_bad_citations"] = run.grounding_rejections
            else:
                note["skipped"] = "No analysis was produced."

        # 6. ACT -------------------------------------------------------------
        with StageRecorder(run, AgentStage.ACT) as note:
            expired = await self.executor.expire_stale()
            note["expired_pending_actions"] = expired
            created = await self._take_actions(run, stored_recommendations)
            created += await self._propose_topics(run, classification.suggested_topics)
            run.actions_created = created
            note["actions_created"] = created

        # 7. VERIFY ----------------------------------------------------------
        await self._verify(run)

        # 8. REPORT ----------------------------------------------------------
        with StageRecorder(run, AgentStage.REPORT) as note:
            run.summary = self._compose_summary(run, analysis)
            note["summary_chars"] = len(run.summary or "")

        if run.status is AgentRunStatus.RUNNING:
            run.status = AgentRunStatus.COMPLETED

    # ------------------------------------------------------------ the stages
    async def _refresh_if_stale(self, *, budget_state: str) -> dict[str, Any]:
        """Top up collection before reasoning, if it is behind and affordable.

        Skipped when the budget is exhausted: reasoning over slightly stale data
        is a much smaller loss than blowing the ceiling that keeps ordinary
        collection running.
        """
        note: dict[str, Any] = {}
        last = await self.db.scalar(
            select(func.max(AccountMetricSnapshot.captured_at)).where(
                AccountMetricSnapshot.x_account_id == self.account.id
            )
        )
        hours = None
        if last is not None:
            captured = last if last.tzinfo else last.replace(tzinfo=UTC)
            hours = (datetime.now(UTC) - captured).total_seconds() / 3600
        note["hours_since_snapshot"] = round(hours, 2) if hours is not None else None

        if budget_state == "exhausted":
            note["skipped"] = "API budget exhausted; reasoning over existing data instead."
            return note
        if hours is not None and hours < STALE_SNAPSHOT_HOURS:
            note["skipped"] = "Collection is current."
            return note

        try:
            from app.collectors.collector import build_collector

            collector = await build_collector(self.db, self.account)
            async with collector.client:
                snapshot_run = await collector.collect_account_snapshot()
                discovery_run = await collector.discover_posts()
            note["snapshot"] = snapshot_run.status.value
            note["discovery"] = discovery_run.status.value
            note["posts_discovered"] = discovery_run.posts_discovered
        except Exception as exc:  # noqa: BLE001 — stale data still supports a useful run
            note["error"] = f"Top-up collection failed: {exc}"[:300]
            log.warning("agent.collect_failed", error=str(exc))
        return note

    async def _store_insights(
        self, run: AgentRun, analysis: AnalysisOutput, evidence: Evidence
    ) -> int:
        rejected = 0
        for proposed in analysis.insights:
            grounding = check_citations(proposed.cited_facts, evidence)
            if not grounding.ok:
                rejected += 1
                log.warning(
                    "agent.insight_rejected",
                    title=proposed.title[:60],
                    problems=grounding.problems,
                )
                continue
            self.db.add(
                Insight(
                    agent_run_id=run.id,
                    x_account_id=self.account.id,
                    kind=proposed.kind,
                    severity=proposed.severity,
                    title=proposed.title,
                    body=proposed.body,
                    # The verified values, taken from the evidence rather than
                    # from the model, so the stored figures are authoritative.
                    supporting_data=dict(grounding.verified),
                    confidence=round(proposed.confidence, 3),
                    provenance=Provenance.INFERRED,
                    period_start=evidence.period_start,
                    period_end=evidence.period_end,
                )
            )
            run.insights_created += 1
        await self.db.flush()
        return rejected

    async def _store_recommendations(
        self, run: AgentRun, analysis: AnalysisOutput, evidence: Evidence, *, days: int
    ) -> list[tuple[Recommendation, ProposedRecommendation]]:
        stored: list[tuple[Recommendation, ProposedRecommendation]] = []
        for proposed in analysis.recommendations:
            grounding = check_citations(proposed.cited_facts, evidence)
            if not grounding.ok:
                run.grounding_rejections += 1
                log.warning(
                    "agent.recommendation_rejected",
                    title=proposed.title[:60],
                    problems=grounding.problems,
                )
                continue

            # Measured here, not asserted by the model — otherwise the thing
            # being predicted and the yardstick would come from the same place.
            baseline = await self.verification.measure_baseline(
                proposed.predicted_metric,
                self.account.id,
                window_days=proposed.verify_after_days,
            )
            recommendation = Recommendation(
                agent_run_id=run.id,
                x_account_id=self.account.id,
                title=proposed.title,
                rationale=proposed.rationale,
                supporting_data=dict(grounding.verified),
                predicted_metric=proposed.predicted_metric,
                predicted_direction=proposed.predicted_direction,
                baseline_value=baseline.value,
                baseline_note=(
                    f"{proposed.predicted_metric.value} over the {proposed.verify_after_days} "
                    f"days before this was proposed, from {baseline.observations} observation(s)."
                    + (f" {baseline.note}" if baseline.note else "")
                ),
                verify_after=date.today() + timedelta(days=proposed.verify_after_days),
                verification_result=VerificationResult.PENDING,
                provenance=Provenance.INFERRED,
            )
            self.db.add(recommendation)
            stored.append((recommendation, proposed))
        await self.db.flush()
        run.recommendations_created = len(stored)
        return stored

    async def _take_actions(
        self,
        run: AgentRun,
        stored: list[tuple[Recommendation, ProposedRecommendation]],
    ) -> int:
        created = 0
        for recommendation, proposed in stored:
            action = proposed.action
            if action is None:
                continue
            if action.action_type not in PROPOSABLE_ACTIONS:
                # The schema restricts this, but the check is repeated here
                # because this is the boundary that matters.
                log.warning("agent.action_not_proposable", action=action.action_type.value)
                continue
            try:
                await self.executor.propose(
                    action.action_type,
                    dict(action.payload),
                    summary=action.summary,
                    agent_run_id=run.id,
                    recommendation_id=recommendation.id,
                    ingested_untrusted_content=run.ingested_untrusted_content,
                )
                created += 1
            except PolicyViolationError as exc:
                log.warning("agent.action_refused", error=str(exc))
        return created

    async def _propose_topics(self, run: AgentRun, suggestions: list[str]) -> int:
        created = 0
        for name in suggestions[:3]:
            try:
                await self.executor.propose(
                    ActionType.PROPOSE_TOPIC,
                    {"name": name, "description": "Suggested by topic classification."},
                    summary=f"Add “{name}” to your content taxonomy",
                    agent_run_id=run.id,
                    ingested_untrusted_content=run.ingested_untrusted_content,
                )
                created += 1
            except PolicyViolationError as exc:
                log.warning("agent.topic_proposal_refused", error=str(exc))
        return created

    async def _verify(self, run: AgentRun) -> None:
        with StageRecorder(run, AgentStage.VERIFY) as note:
            graded = await self.verification.grade_due(self.account.id)
            note["graded"] = len(graded)
            note["confirmed"] = sum(
                1 for r in graded if r.verification_result is VerificationResult.CONFIRMED
            )
            note["refuted"] = sum(
                1 for r in graded if r.verification_result is VerificationResult.REFUTED
            )
            note["inconclusive"] = sum(
                1 for r in graded if r.verification_result is VerificationResult.INCONCLUSIVE
            )

    def _compose_summary(self, run: AgentRun, analysis: AnalysisOutput | None) -> str:
        if analysis is None:
            return (
                "The deterministic analysis ran and is stored, but the reasoning step "
                "did not complete, so there are no new insights from this cycle. "
                f"Reason: {run.error or 'unknown'}."
            )
        if analysis.insufficient_data_note:
            return analysis.insufficient_data_note
        parts = [analysis.summary]
        if run.grounding_rejections:
            parts.append(
                f"{run.grounding_rejections} proposed item(s) were discarded because the "
                f"figures they cited did not match the evidence."
            )
        return "\n\n".join(parts)


class StageRecorder:
    """Context manager recording which stage ran, for how long, and what it found."""

    def __init__(self, run: AgentRun, stage: AgentStage) -> None:
        self.run = run
        self.stage = stage
        self.note: dict[str, Any] = {}

    def __enter__(self) -> dict[str, Any]:
        self.run.stage = self.stage
        self._started = time.monotonic()
        return self.note

    def __exit__(self, *exc: object) -> None:
        self.note["ms"] = int((time.monotonic() - self._started) * 1000)
        # Reassigned rather than mutated: SQLAlchemy does not track in-place
        # changes to a JSON column, and the stage log would silently not persist.
        self.run.stage_log = {**self.run.stage_log, self.stage.value: self.note}
