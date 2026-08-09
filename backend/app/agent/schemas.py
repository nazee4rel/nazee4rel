"""Structured-output contracts for every call the agent makes to Claude.

These Pydantic models are passed to the Messages API as `output_format`, so the
response is constrained to this shape rather than parsed hopefully out of prose.
That matters here for a specific reason: the model's output is *data*, and the
only thing the executor will act on is an enum member from a closed set defined
in `app.agent.policy`. There is no path from free text to an action.

Two conventions carry the "don't invent data" rule into the schema itself:

* Every insight and recommendation must cite facts by key **and** by value. The
  keys come from the evidence bundle the model was given; the values are checked
  against it afterwards. A misquoted figure is a rejection, not a correction.
* Predictions are restricted to metrics this system can actually measure, so a
  recommendation cannot be worded in a way that escapes later grading.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.agent import (
    ActionType,
    InsightKind,
    InsightSeverity,
    PredictedDirection,
    PredictedMetric,
)

# Actions the model is permitted to *propose*. PUBLISH_POST is deliberately
# absent: publishing is only ever reached by a human promoting an approved
# draft, so no single model output can put a post in front of the approval
# queue as a publish.
PROPOSABLE_ACTIONS = (
    ActionType.RECORD_NOTE,
    ActionType.RAISE_ALERT,
    ActionType.DRAFT_POST,
    ActionType.PROPOSE_TOPIC,
)


class CitedFact(BaseModel):
    """One figure quoted from the evidence bundle.

    `value` is what the model believes the figure to be. It is checked against
    the evidence, which is how a fabricated or garbled number is caught — the
    key alone would not catch "followers grew 400" when the evidence says 40.
    """

    key: str = Field(
        description="Exact fact key from the evidence bundle, e.g. 'growth.median_daily'."
    )
    value: str = Field(description="The value of that fact, exactly as it appears in the evidence.")


class ProposedAction(BaseModel):
    action_type: ActionType = Field(
        description="Must be one of RECORD_NOTE, RAISE_ALERT, DRAFT_POST, PROPOSE_TOPIC."
    )
    summary: str = Field(max_length=280, description="One line describing what this action does.")
    payload: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Fields for this action: RECORD_NOTE/RAISE_ALERT take 'message'; "
            "DRAFT_POST takes 'text'; PROPOSE_TOPIC takes 'name' and 'description'."
        ),
    )


class ProposedInsight(BaseModel):
    kind: InsightKind
    severity: InsightSeverity
    title: str = Field(max_length=200)
    body: str = Field(
        max_length=1200,
        description=(
            "What is happening and why it matters. State sample sizes. Where a "
            "figure is unavailable say so — never write 0 for a missing value."
        ),
    )
    cited_facts: list[CitedFact] = Field(
        min_length=1,
        description=(
            "Every figure this insight relies on. An insight with no facts behind it is not wanted."
        ),
    )
    confidence: float = Field(ge=0.0, le=1.0)


class ProposedRecommendation(BaseModel):
    title: str = Field(max_length=200)
    rationale: str = Field(
        max_length=1200, description="Why this follows from the evidence, citing figures."
    )
    cited_facts: list[CitedFact] = Field(min_length=1)
    predicted_metric: PredictedMetric = Field(
        description="Which measurable metric you expect to move if this is followed."
    )
    predicted_direction: PredictedDirection
    verify_after_days: int = Field(
        ge=3,
        le=90,
        description=(
            "Days before this can fairly be graded. Long enough that normal "
            "variation does not decide the outcome."
        ),
    )
    action: ProposedAction | None = Field(
        default=None, description="An optional concrete step. Omit when the advice needs no action."
    )


class AnalysisOutput(BaseModel):
    """The REASON stage's complete response."""

    summary: str = Field(
        max_length=1200,
        description="A short brief for the account owner: what changed, and what to do about it.",
    )
    insights: list[ProposedInsight] = Field(default_factory=list, max_length=8)
    recommendations: list[ProposedRecommendation] = Field(default_factory=list, max_length=5)
    # An honest empty answer is a valid answer. Without this field the model is
    # pushed to manufacture findings from thin data.
    insufficient_data_note: str | None = Field(
        default=None,
        description=(
            "Set this instead of guessing when the evidence cannot support any "
            "finding, and leave the lists empty."
        ),
    )


class TopicAssignment(BaseModel):
    post_ref: str = Field(description="The post_ref given in the input list.")
    topic_names: list[str] = Field(
        max_length=3,
        description=(
            "Topics from the supplied taxonomy only, most relevant first. Return "
            "an empty list rather than forcing a poor fit."
        ),
    )
    confidence: float = Field(ge=0.0, le=1.0)


class TopicClassificationOutput(BaseModel):
    assignments: list[TopicAssignment] = Field(default_factory=list)
    # Surfaces genuine gaps in the taxonomy without letting the model widen it
    # unilaterally — these become T1 approval requests.
    suggested_new_topics: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Only where several posts fit no existing topic. Suggestions, not additions.",
    )
