"""The autonomy policy: what the agent may do, and under what conditions.

This is the security boundary of Phase 6, kept in one short file on purpose. If
you want to know what this system can do to your account, you should not have to
read the agent to find out — you should be able to read this.

Two rules are structural rather than advisory:

* An action type that is not in `TIERS` cannot be executed. The executor looks
  its tier up and refuses on `KeyError`, so a model that invents an action name
  fails closed rather than falling through to a default.
* `T3` names the class of action this system refuses to implement — deleting
  posts, changing account settings, spending money. There is no code path that
  performs one, and no setting that enables one. It is listed so the boundary is
  written down rather than merely absent.

The brief's prohibitions map onto this file as follows:

    Access passwords            no action reads credentials; the executor has
                                no access to the token store at all
    Bypass X security           the only outward action goes through the
                                declared X endpoint registry, with the user's
                                own OAuth token and scopes
    Scrape private accounts     no action fetches another user's data
    Circumvent API restrictions rate limiting and the cost governor sit in the
                                X client, below this layer
    Unauthorised actions        T1/T2 require an explicit approval row
    Spend money                 no action type spends; T3 by definition
    Irreversible changes        no delete or edit action exists
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.agent import ActionType, AutonomyTier
from app.models.enums import XCapability

# How long an approval request stays actionable. An action approved a week
# after it was proposed would act on stale analysis, so pending actions lapse
# rather than waiting indefinitely.
APPROVAL_TTL_HOURS = 24

# Longest post text the agent will ever draft or publish. X's own limit varies
# by subscription; the conservative value is the one that is always valid.
MAX_POST_CHARS = 280


@dataclass(frozen=True)
class ActionPolicy:
    tier: AutonomyTier
    # Human-readable statement of the effect, shown on the approval screen.
    # Written here rather than by the model: the description of what you are
    # approving must not be attacker-influenced.
    effect: str
    outward_facing: bool = False
    # Capability that must be AVAILABLE before the action can execute.
    requires_capability: XCapability | None = None
    # Requires X_ENABLE_WRITE_ACTIONS. When that flag is off the app never
    # requests `tweet.write`, so the token physically cannot publish.
    requires_write_flag: bool = False


TIERS: dict[ActionType, ActionPolicy] = {
    ActionType.RECORD_NOTE: ActionPolicy(
        tier=AutonomyTier.T0,
        effect="Records a note against the account. Visible only in this dashboard.",
    ),
    ActionType.RAISE_ALERT: ActionPolicy(
        tier=AutonomyTier.T0,
        effect="Raises an alert in this dashboard. Sends nothing to X.",
    ),
    ActionType.DRAFT_POST: ActionPolicy(
        tier=AutonomyTier.T0,
        effect="Saves a draft post for you to review. Nothing is published.",
    ),
    ActionType.PROPOSE_TOPIC: ActionPolicy(
        # Adding to the taxonomy needs a human because uncontrolled growth in
        # topic labels is what makes cross-period comparison meaningless — the
        # one thing topic analysis exists for.
        tier=AutonomyTier.T1,
        effect="Adds a new topic to your content taxonomy. Affects how future posts "
        "are categorised, not how past ones were.",
    ),
    ActionType.PUBLISH_POST: ActionPolicy(
        tier=AutonomyTier.T2,
        effect="Publishes a post to your X account. This is public and this system "
        "cannot delete it afterwards.",
        outward_facing=True,
        requires_capability=XCapability.WRITE_POSTS,
        requires_write_flag=True,
    ),
}

# Named for documentation. Nothing dispatches on this list — these actions have
# no implementation anywhere in the codebase, which is the actual guarantee.
FORBIDDEN_ACTIONS: tuple[str, ...] = (
    "DELETE_POST",
    "EDIT_POST",
    "FOLLOW_USER",
    "UNFOLLOW_USER",
    "BLOCK_USER",
    "SEND_DIRECT_MESSAGE",
    "CHANGE_ACCOUNT_SETTINGS",
    "PURCHASE_API_CREDIT",
    "RUN_PAID_PROMOTION",
)


class PolicyViolationError(Exception):
    """Raised when an action fails a gate. Always fatal to that action."""


def policy_for(action_type: ActionType | str) -> ActionPolicy:
    """Look up an action's policy, refusing anything undeclared."""
    try:
        key = action_type if isinstance(action_type, ActionType) else ActionType(action_type)
    except ValueError:
        raise PolicyViolationError(
            f"{action_type!r} is not an action this system implements. "
            f"Allowed: {', '.join(sorted(a.value for a in ActionType))}."
        ) from None
    try:
        return TIERS[key]
    except KeyError:  # pragma: no cover — every member is in TIERS, asserted by a test
        raise PolicyViolationError(f"No policy declared for {key.value}.") from None


def requires_approval(action_type: ActionType) -> bool:
    return policy_for(action_type).tier is not AutonomyTier.T0


def validate_payload(action_type: ActionType, payload: dict[str, Any]) -> dict[str, str]:
    """Check and normalise an action payload.

    Returns the cleaned payload as strings. Raises `PolicyViolationError` rather than
    coercing: a payload that does not fit its action is a signal that something
    went wrong upstream, and guessing at intent is exactly the wrong response.
    """
    match action_type:
        case ActionType.RECORD_NOTE | ActionType.RAISE_ALERT:
            text = str(payload.get("message", "")).strip()
            if not text:
                raise PolicyViolationError(f"{action_type.value} requires a non-empty 'message'.")
            return {"message": text[:2000]}

        case ActionType.DRAFT_POST | ActionType.PUBLISH_POST:
            text = str(payload.get("text", "")).strip()
            if not text:
                raise PolicyViolationError(f"{action_type.value} requires non-empty 'text'.")
            if len(text) > MAX_POST_CHARS:
                raise PolicyViolationError(
                    f"Post text is {len(text)} characters; the limit is {MAX_POST_CHARS}."
                )
            return {"text": text}

        case ActionType.PROPOSE_TOPIC:
            name = str(payload.get("name", "")).strip()
            if not name:
                raise PolicyViolationError("PROPOSE_TOPIC requires a 'name'.")
            if len(name) > 80:
                raise PolicyViolationError("Topic names are limited to 80 characters.")
            return {
                "name": name,
                "description": str(payload.get("description", "")).strip()[:500],
            }

    raise PolicyViolationError(f"No payload validator for {action_type.value}.")  # pragma: no cover
