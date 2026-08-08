"""Follower attribution: which posts plausibly drove follower growth.

**This module produces estimates, not measurements.** X exposes a follower
*count* and no follower *event stream* — there is no record of who followed you,
when, or from where. So the question "which post gained me followers?" cannot be
answered directly at any access level. It can only be modelled.

The model
---------
Hourly follower deltas form a signal. Each post contributes a decaying response
starting at its publication time. Attribution is then a deconvolution: find the
non-negative per-post contributions that best explain growth above baseline.

    delta[t] = baseline + Σ_i  c_i · response(t − t_i)  + noise,   c_i ≥ 0

Solved as ridge-regularised non-negative least squares. Non-negativity encodes
a real constraint — a post cannot cause negative followers — and ridge keeps the
fit stable when posts overlap in time.

Why the confidence intervals matter more than the point estimates
-----------------------------------------------------------------
Posts published close together produce nearly identical response curves, so the
data genuinely cannot say which one did the work. That shows up as wide,
overlapping intervals. Bootstrapped residuals give intervals that widen honestly
in exactly that case, and the report says so in words rather than quietly
picking a winner.

Every output carries `Provenance.INFERRED` and must be rendered differently from
measured data.
"""

from __future__ import annotations

import enum
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
from scipy.optimize import nnls

from app.models.enums import Provenance

# Most of a post's follower effect lands within a day or so of publication.
DEFAULT_DECAY_HOURS = 12.0
# Beyond this the response is negligible and only adds collinearity.
RESPONSE_HORIZON_HOURS = 72.0

# Below three days of hourly data, "attribution" is pattern-matching on noise.
MIN_HOURS_OF_HISTORY = 72
MIN_POSTS = 2

# Ridge strength, expressed *relative to the design matrix scale*.
#
# An absolute lambda cannot work here: the response curves are normalised to
# sum to 1, so each column's squared norm is around 0.04, and a fixed lambda of
# 1.0 would outweigh the data by roughly 25x. That crushes the coefficients and
# pushes real signal into the residuals — which then breaks the bootstrap, since
# it resamples those residuals as if they were noise. Scaling to the design
# keeps the penalty proportionate whatever the window length.
RIDGE_ALPHA = 0.01
BOOTSTRAP_ITERATIONS = 300
CI_LOWER_PERCENTILE = 5.0
CI_UPPER_PERCENTILE = 95.0


class AttributionConfidence(enum.StrEnum):
    NONE = "NONE"  # refused — not enough data to model anything
    LOW = "LOW"  # produced, but treat as a weak hint
    MODERATE = "MODERATE"
    GOOD = "GOOD"


@dataclass(frozen=True)
class PostContribution:
    post_id: str
    posted_at: datetime
    estimate: float
    ci_low: float
    ci_high: float
    share_of_explained: float

    @property
    def is_distinguishable(self) -> bool:
        """True when the interval excludes zero.

        When it does not, the honest statement is "this post cannot be shown to
        have driven growth" — not "this post drove zero followers".
        """
        return self.ci_low > 0.5

    @property
    def interval_width_ratio(self) -> float | None:
        """Interval width relative to the estimate. Large means unidentifiable."""
        if self.estimate <= 0:
            return None
        return (self.ci_high - self.ci_low) / self.estimate


@dataclass
class AttributionReport:
    contributions: list[PostContribution] = field(default_factory=list)
    baseline_per_hour: float = 0.0
    total_observed_growth: float = 0.0
    total_explained: float = 0.0
    unexplained: float = 0.0
    confidence: AttributionConfidence = AttributionConfidence.NONE
    hours_of_history: int = 0
    caveats: list[str] = field(default_factory=list)
    # Never anything else. This is modelled, not measured.
    provenance: Provenance = Provenance.INFERRED

    @property
    def is_usable(self) -> bool:
        return self.confidence is not AttributionConfidence.NONE

    @property
    def distinguishable_contributions(self) -> list[PostContribution]:
        return [c for c in self.contributions if c.is_distinguishable]

    def summary(self) -> str:
        if not self.is_usable:
            return "Attribution unavailable: " + (
                self.caveats[0] if self.caveats else "insufficient data."
            )
        clear = len(self.distinguishable_contributions)
        return (
            f"{self.total_observed_growth:.0f} followers gained over "
            f"{self.hours_of_history}h. {clear} of {len(self.contributions)} posts show a "
            f"distinguishable effect ({self.confidence.value.lower()} confidence)."
        )


def response_curve(hours_since_post: np.ndarray, decay_hours: float) -> np.ndarray:
    """Normalised exponential decay, zero before publication.

    Normalised so a contribution coefficient reads directly as "followers
    attributable to this post" rather than as an arbitrary scale factor.
    """
    response = np.where(
        (hours_since_post >= 0) & (hours_since_post <= RESPONSE_HORIZON_HOURS),
        np.exp(-np.maximum(hours_since_post, 0) / decay_hours),
        0.0,
    )
    total = response.sum()
    return response / total if total > 0 else response


def _hourly_series(
    snapshots: list[tuple[datetime, int]],
) -> tuple[list[datetime], np.ndarray]:
    """Follower deltas between consecutive hourly snapshots."""
    ordered = sorted(snapshots, key=lambda s: s[0])
    times: list[datetime] = []
    deltas: list[float] = []

    for (t0, c0), (t1, c1) in zip(ordered, ordered[1:], strict=False):
        gap_hours = (t1 - t0).total_seconds() / 3600
        if gap_hours <= 0:
            continue
        # A long gap means collection was down. Spreading the delta evenly
        # across it would invent a growth pattern that was never observed, so
        # the interval is dropped instead.
        if gap_hours > 3:
            continue
        times.append(t1)
        deltas.append((c1 - c0) / gap_hours)

    return times, np.asarray(deltas, dtype=float)


def attribute_followers(
    follower_snapshots: list[tuple[datetime, int]],
    posts: list[tuple[str, datetime]],
    *,
    decay_hours: float = DEFAULT_DECAY_HOURS,
    now: datetime | None = None,
) -> AttributionReport:
    """Apportion follower growth across posts.

    `follower_snapshots` is (timestamp, follower_count), ideally hourly.
    `posts` is (post_id, posted_at).
    """
    now = now or datetime.now(UTC)
    report = AttributionReport()

    times, deltas = _hourly_series(follower_snapshots)
    report.hours_of_history = len(times)

    # --- refusal conditions ------------------------------------------------
    if len(times) < MIN_HOURS_OF_HISTORY:
        report.caveats.append(
            f"Only {len(times)} usable hourly observations. At least "
            f"{MIN_HOURS_OF_HISTORY} are needed before follower attribution is "
            f"anything more than noise-fitting. Collection continues; this will "
            f"become available."
        )
        return report

    relevant = [
        (post_id, posted_at)
        for post_id, posted_at in posts
        if times[0] - timedelta(hours=RESPONSE_HORIZON_HOURS) <= posted_at <= times[-1]
    ]
    if len(relevant) < MIN_POSTS:
        report.caveats.append(
            f"Only {len(relevant)} post(s) fall inside the observed window; "
            f"there is nothing to apportion between."
        )
        return report

    # --- baseline ----------------------------------------------------------
    # Median, not mean: a viral hour must not raise the "normal" it is being
    # measured against.
    baseline = float(np.median(deltas))
    report.baseline_per_hour = baseline
    excess = deltas - baseline
    report.total_observed_growth = float(deltas.sum())

    # --- design matrix -----------------------------------------------------
    time_array = np.array([(t - times[0]).total_seconds() / 3600 for t in times])
    design = np.zeros((len(times), len(relevant)))
    for index, (_post_id, posted_at) in enumerate(relevant):
        offset = (posted_at - times[0]).total_seconds() / 3600
        design[:, index] = response_curve(time_array - offset, decay_hours)

    # Posts whose response is entirely outside the observed window contribute
    # nothing and would only make the system singular.
    active = design.sum(axis=0) > 1e-9
    if not active.any():
        report.caveats.append("No post's response window overlaps the observed follower data.")
        return report

    design = design[:, active]
    relevant = [p for p, keep in zip(relevant, active, strict=False) if keep]

    # --- ridge-regularised NNLS -------------------------------------------
    # Ridge implemented by augmentation, which keeps `nnls` applicable and so
    # preserves the non-negativity constraint.
    mean_column_energy = float(np.mean((design**2).sum(axis=0)))
    ridge_lambda = RIDGE_ALPHA * max(mean_column_energy, 1e-12)
    augmented_design = np.vstack([design, np.sqrt(ridge_lambda) * np.eye(design.shape[1])])
    augmented_target = np.concatenate([excess, np.zeros(design.shape[1])])

    try:
        coefficients, _residual = nnls(augmented_design, augmented_target)
    except Exception:  # noqa: BLE001
        report.caveats.append("The attribution model failed to converge on this data.")
        return report

    fitted = design @ coefficients
    residuals = excess - fitted

    # --- bootstrap confidence intervals -----------------------------------
    # Residual resampling rather than a parametric interval: follower deltas are
    # small integer counts and nothing here is normally distributed.
    rng = np.random.default_rng(seed=20260808)
    samples = np.zeros((BOOTSTRAP_ITERATIONS, design.shape[1]))
    for iteration in range(BOOTSTRAP_ITERATIONS):
        resampled = fitted + rng.choice(residuals, size=len(residuals), replace=True)
        target = np.concatenate([resampled, np.zeros(design.shape[1])])
        try:
            samples[iteration], _ = nnls(augmented_design, target)
        except Exception:  # noqa: BLE001
            samples[iteration] = coefficients

    # The bootstrap measures *dispersion*, and the interval is then centred on
    # the point estimate rather than taken as raw percentiles.
    #
    # This matters because the estimator is penalised: every bootstrap refit
    # applies ridge shrinkage a second time, on top of the shrinkage already
    # baked into the fitted values it resamples around. Raw percentiles are
    # therefore biased low — badly enough that the point estimate can fall
    # outside its own interval, which is indefensible in something a user reads.
    # Taking the spread and re-centring keeps the interval honest about
    # uncertainty without inheriting that bias.
    centre = samples.mean(axis=0)
    # Spreads are clamped non-negative. For a post whose bootstrap samples are
    # mostly zero with occasional large values, the mean can sit above the 95th
    # percentile, which would otherwise produce an upper bound below the point
    # estimate. Clamping guarantees lower <= estimate <= upper always holds.
    spread_below = np.maximum(0.0, centre - np.percentile(samples, CI_LOWER_PERCENTILE, axis=0))
    spread_above = np.maximum(0.0, np.percentile(samples, CI_UPPER_PERCENTILE, axis=0) - centre)

    # Lower bound clamped at zero too: a post cannot contribute negative followers.
    lower = np.maximum(0.0, coefficients - spread_below)
    upper = coefficients + spread_above

    explained = float(coefficients.sum())
    report.total_explained = explained
    report.unexplained = float(report.total_observed_growth - baseline * len(times) - explained)

    report.contributions = [
        PostContribution(
            post_id=post_id,
            posted_at=posted_at,
            estimate=float(coefficients[index]),
            ci_low=float(lower[index]),
            ci_high=float(upper[index]),
            share_of_explained=(float(coefficients[index] / explained) if explained > 0 else 0.0),
        )
        for index, (post_id, posted_at) in enumerate(relevant)
    ]
    report.contributions.sort(key=lambda c: c.estimate, reverse=True)

    report.confidence = _assess_confidence(report, design, len(times))
    report.caveats.extend(_build_caveats(report, relevant))
    return report


def _assess_confidence(
    report: AttributionReport, design: np.ndarray, observations: int
) -> AttributionConfidence:
    """How much the numbers deserve to be trusted.

    Driven by data volume and by how separable the posts are. Posts published
    close together give near-identical response curves, which makes the system
    ill-conditioned — the fit still returns numbers, but they are not evidence.
    """
    try:
        condition = float(np.linalg.cond(design.T @ design))
    except Exception:  # noqa: BLE001
        condition = float("inf")

    if condition > 1e8 or observations < MIN_HOURS_OF_HISTORY * 2:
        return AttributionConfidence.LOW

    widths = [
        c.interval_width_ratio for c in report.contributions if c.interval_width_ratio is not None
    ]
    typical_width = statistics.median(widths) if widths else float("inf")

    if condition < 1e4 and typical_width < 1.0 and observations >= 336:  # ~2 weeks
        return AttributionConfidence.GOOD
    if typical_width < 2.0:
        return AttributionConfidence.MODERATE
    return AttributionConfidence.LOW


def _build_caveats(report: AttributionReport, posts: list[tuple[str, datetime]]) -> list[str]:
    caveats = [
        "These figures are modelled, not measured. X provides no follower-event "
        "data, so no system can tell you with certainty which post gained you a "
        "follower — including this one."
    ]

    indistinguishable = len(report.contributions) - len(report.distinguishable_contributions)
    if indistinguishable:
        caveats.append(
            f"{indistinguishable} post(s) have confidence intervals spanning zero. "
            f"That means the data cannot show they had an effect — which is not "
            f"the same as showing they had none."
        )

    # Posts within a few hours of each other are effectively inseparable.
    ordered = sorted(posts, key=lambda p: p[1])
    close_pairs = sum(
        1
        for a, b in zip(ordered, ordered[1:], strict=False)
        if (b[1] - a[1]).total_seconds() / 3600 < 3
    )
    if close_pairs:
        caveats.append(
            f"{close_pairs} pair(s) of posts were published within three hours of "
            f"each other. Their response curves overlap almost entirely, so the "
            f"split between them is largely arbitrary — read them as a group."
        )

    if report.unexplained > report.total_explained:
        caveats.append(
            "More growth is unexplained than attributed. Followers arrive for "
            "reasons outside your posting — search, recommendations, mentions by "
            "others — and the model does not attempt to invent a cause for those."
        )

    if report.confidence is AttributionConfidence.LOW:
        caveats.append(
            "Confidence is low. Treat the ranking as a weak hint rather than a "
            "finding, and revisit once more history has accumulated."
        )

    return caveats
