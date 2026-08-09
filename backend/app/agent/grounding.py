"""Verification that a model's claims match the evidence it was given.

The rule the brief states — do not invent data — cannot be enforced by asking
nicely. It is enforced here: every insight and recommendation names the figures
it rests on, and this module checks each one against the evidence bundle before
anything is stored.

The check is deliberately strict. One bad citation discards the whole item
rather than trimming the bad citation and keeping the prose, because the prose
is what the user reads and it was written around that figure. A rewritten
version with the number quietly dropped would be worse than nothing: it would
look verified.

Three failure modes are caught:

* **A key that does not exist.** The figure was invented outright.
* **A value that does not match.** The key is real but the number is wrong —
  "followers grew 400" when the evidence says 40.
* **A number quoted for something unavailable.** The evidence says a figure
  could not be obtained and the model supplied one anyway. This is the failure
  this whole project is organised around, so it is checked explicitly rather
  than falling out of the generic comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.evidence import Evidence, FactValue, format_fact
from app.agent.schemas import CitedFact

# Relative tolerance on numeric citations. Wide enough to forgive a rounded
# restatement (0.0432 for 0.04321), tight enough that a different figure fails.
RELATIVE_TOLERANCE = 0.005
ABSOLUTE_TOLERANCE = 1e-9

_UNAVAILABLE_WORDS = {"unavailable", "none", "null", "n/a", "unknown", "not available"}


@dataclass
class GroundingResult:
    ok: bool
    # Values taken from the evidence, not from the model. Even when a citation
    # matches, the stored figure is the authoritative one.
    verified: dict[str, FactValue] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _as_number(text: str) -> float | None:
    cleaned = text.strip().replace(",", "").replace("%", "").lstrip("+")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _values_match(claimed: str, actual: FactValue) -> bool:
    claimed_text = claimed.strip()

    if actual is None:
        # The evidence says this is unavailable. Only an explicit statement of
        # unavailability is an acceptable citation; a number here is exactly
        # the fabrication we are looking for.
        return claimed_text.lower() in _UNAVAILABLE_WORDS

    if claimed_text.lower() in _UNAVAILABLE_WORDS:
        # The reverse: claiming absence for something that was measured.
        return False

    if format_fact(actual) == claimed_text:
        return True

    if isinstance(actual, bool):
        return claimed_text.lower() in ({"true", "yes"} if actual else {"false", "no"})

    if isinstance(actual, int | float):
        claimed_number = _as_number(claimed_text)
        if claimed_number is None:
            return False
        difference = abs(claimed_number - float(actual))
        return difference <= max(ABSOLUTE_TOLERANCE, RELATIVE_TOLERANCE * abs(float(actual)))

    # Strings compare case-insensitively; label capitalisation is not a lie.
    return claimed_text.casefold() == str(actual).casefold()


def check_citations(cited: list[CitedFact], evidence: Evidence) -> GroundingResult:
    """Verify every citation on one insight or recommendation."""
    result = GroundingResult(ok=True)

    if not cited:
        result.ok = False
        result.problems.append("No figures were cited, so nothing could be verified.")
        return result

    for citation in cited:
        key = citation.key.strip()
        if key not in evidence.facts:
            result.ok = False
            result.problems.append(f"cited an unknown fact key {key!r}")
            continue

        actual = evidence.facts[key]
        if not _values_match(citation.value, actual):
            result.ok = False
            if actual is None:
                result.problems.append(
                    f"quoted {citation.value!r} for {key!r}, which the evidence records "
                    f"as unavailable"
                )
            else:
                result.problems.append(
                    f"quoted {citation.value!r} for {key!r}, which is {format_fact(actual)!r}"
                )
            continue

        result.verified[key] = actual

    return result
