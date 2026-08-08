import ProvenanceBadge from "@/components/ProvenanceBadge";
import {
  METRIC_LABELS,
  SEVERITY_TONE,
  VERDICT_TONE,
  formatMetricValue,
  type AgentInsight,
  type AgentRecommendation,
  type AgentRun,
} from "@/components/agent";
import { apiFetch, getAccountsStatus } from "@/lib/api";

import RecommendationDecision from "./RecommendationDecision";
import RunAgentButton from "./RunAgentButton";

export default async function InsightsPage() {
  const status = await getAccountsStatus();
  const account = status?.accounts[0];

  if (!account) {
    return (
      <div className="rounded-xl border border-dashed border-border bg-surface/50 p-6 text-sm text-text-muted">
        Connect an X account before the agent has anything to reason about.
      </div>
    );
  }

  const [insightsResult, recommendationsResult, runsResult] = await Promise.all([
    apiFetch<AgentInsight[]>(`/api/v1/agent/${account.id}/insights?limit=30`),
    apiFetch<AgentRecommendation[]>(`/api/v1/agent/${account.id}/recommendations?limit=30`),
    apiFetch<AgentRun[]>(`/api/v1/agent/${account.id}/runs?limit=1`),
  ]);

  const insights = insightsResult.ok ? insightsResult.data : [];
  const recommendations = recommendationsResult.ok ? recommendationsResult.data : [];
  const latestRun = runsResult.ok ? (runsResult.data[0] ?? null) : null;

  const graded = recommendations.filter((r) => r.verification_result !== "PENDING");
  const confirmed = graded.filter((r) => r.verification_result === "CONFIRMED").length;
  const refuted = graded.filter((r) => r.verification_result === "REFUTED").length;

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">AI Insights</h1>
          <p className="mt-1 text-sm text-text-muted">
            What is working, what is not, and what to do next — each claim carrying
            the figures it was drawn from.
          </p>
        </div>
        <RunAgentButton accountId={account.id} />
      </header>

      <section className="rounded-xl border border-border bg-surface p-5">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="text-sm font-medium">How to read this page</h2>
          <ProvenanceBadge provenance="INFERRED" />
        </div>
        <p className="mt-3 max-w-3xl text-sm leading-relaxed text-text-muted">
          The numbers are computed by the analytics engine; the model only
          interprets them. Every insight below had to cite the exact figures it
          relied on, and those citations were checked against the data before the
          insight was stored — an item quoting a figure the data does not contain
          is discarded rather than published.
          {latestRun?.grounding_rejections ? (
            <>
              {" "}
              <strong className="text-warning">
                {latestRun.grounding_rejections} item(s) were discarded on the last
                run for exactly that reason.
              </strong>
            </>
          ) : null}
        </p>
        {latestRun && (
          <p className="mt-3 text-xs text-text-muted">
            Last run {new Date(latestRun.started_at).toLocaleString()} ·{" "}
            {latestRun.status.toLowerCase()}
            {latestRun.model ? ` · ${latestRun.model}` : ""}
            {graded.length > 0 && (
              <>
                {" "}
                · past advice graded: {confirmed} confirmed, {refuted} refuted,{" "}
                {graded.length - confirmed - refuted} inconclusive
              </>
            )}
          </p>
        )}
      </section>

      {latestRun?.summary && (
        <section className="rounded-xl border border-accent/30 bg-accent/5 p-5">
          <h2 className="text-sm font-medium">Latest brief</h2>
          <p className="mt-2 whitespace-pre-line text-sm leading-relaxed">
            {latestRun.summary}
          </p>
        </section>
      )}

      {/* ----------------------------------------------------------- insights */}
      <section className="space-y-3">
        <h2 className="text-sm font-medium">Insights</h2>
        {insights.length === 0 ? (
          <p className="rounded-xl border border-dashed border-border bg-surface/50 p-6 text-sm text-text-muted">
            No insights yet. The agent runs daily, and declines to run at all until
            there is enough history to say something honest — reasoning over a
            handful of data points produces confident advice from noise.
          </p>
        ) : (
          <div className="space-y-3">
            {insights.map((insight) => (
              <article
                key={insight.id}
                className="rounded-xl border border-border bg-surface p-5"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    className={`rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
                      SEVERITY_TONE[insight.severity] ?? SEVERITY_TONE.INFO
                    }`}
                  >
                    {insight.severity.toLowerCase()}
                  </span>
                  <span className="text-xs uppercase tracking-wide text-text-muted">
                    {insight.kind.replace(/_/g, " ").toLowerCase()}
                  </span>
                  <span className="ml-auto text-xs text-text-muted">
                    {new Date(insight.created_at).toLocaleDateString()}
                  </span>
                </div>

                <h3 className="mt-2 text-sm font-medium">{insight.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-text-muted">{insight.body}</p>

                <SupportingData data={insight.supporting_data} />

                {insight.confidence !== null && (
                  <p className="mt-2 text-xs text-text-muted">
                    Stated confidence {(insight.confidence * 100).toFixed(0)}% — the
                    model&apos;s own, not a measured error bar.
                  </p>
                )}
              </article>
            ))}
          </div>
        )}
      </section>

      {/* ---------------------------------------------------- recommendations */}
      <section className="space-y-3">
        <h2 className="text-sm font-medium">Recommendations, as predictions</h2>
        <p className="max-w-3xl text-xs leading-relaxed text-text-muted">
          Each one names a metric this system can measure, a direction, and a date
          after which it can fairly be checked. The baseline was measured when the
          recommendation was made, not asserted by the model. Advice that cannot be
          shown wrong cannot be learned from.
        </p>

        {recommendations.length === 0 ? (
          <p className="rounded-xl border border-dashed border-border bg-surface/50 p-6 text-sm text-text-muted">
            None yet.
          </p>
        ) : (
          <div className="space-y-3">
            {recommendations.map((recommendation) => (
              <article
                key={recommendation.id}
                className="rounded-xl border border-border bg-surface p-5"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    className={`rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
                      VERDICT_TONE[recommendation.verification_result] ?? VERDICT_TONE.PENDING
                    }`}
                  >
                    {recommendation.verification_result.toLowerCase()}
                  </span>
                  <span className="ml-auto text-xs text-text-muted">
                    {recommendation.verification_result === "PENDING"
                      ? `checked after ${recommendation.verify_after}`
                      : `checked on ${recommendation.verify_after}`}
                  </span>
                </div>

                <h3 className="mt-2 text-sm font-medium">{recommendation.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-text-muted">
                  {recommendation.rationale}
                </p>

                <div className="mt-3 rounded-lg border border-border bg-surface-raised/50 p-3 text-xs leading-relaxed text-text-muted">
                  <p>
                    <strong className="text-text">The prediction:</strong>{" "}
                    {METRIC_LABELS[recommendation.predicted_metric] ??
                      recommendation.predicted_metric}{" "}
                    will {recommendation.predicted_direction.toLowerCase()}, from a
                    baseline of{" "}
                    <span className="numeric">
                      {formatMetricValue(
                        recommendation.predicted_metric,
                        recommendation.baseline_value,
                      )}
                    </span>
                    {recommendation.observed_value !== null && (
                      <>
                        {" "}
                        → observed{" "}
                        <span className="numeric">
                          {formatMetricValue(
                            recommendation.predicted_metric,
                            recommendation.observed_value,
                          )}
                        </span>
                      </>
                    )}
                    .
                  </p>
                  {recommendation.baseline_note && (
                    <p className="mt-1.5">{recommendation.baseline_note}</p>
                  )}
                  {recommendation.verification_note && (
                    <p className="mt-1.5 text-text">{recommendation.verification_note}</p>
                  )}
                </div>

                <SupportingData data={recommendation.supporting_data} />

                <div className="mt-3">
                  <RecommendationDecision
                    recommendationId={recommendation.id}
                    status={recommendation.status}
                  />
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

/**
 * The figures behind a claim.
 *
 * Shown rather than hidden: these values come from the evidence bundle, not from
 * the model's reply, so they are the auditable part of the assertion above them.
 */
function SupportingData({
  data,
}: {
  data: Record<string, string | number | boolean | null>;
}) {
  const entries = Object.entries(data);
  if (entries.length === 0) return null;

  return (
    <details className="mt-3">
      <summary className="cursor-pointer text-xs text-text-muted">
        Figures this rests on ({entries.length})
      </summary>
      <dl className="mt-2 grid gap-1.5 sm:grid-cols-2">
        {entries.map(([key, value]) => (
          <div key={key} className="flex items-baseline gap-2 text-xs">
            <dt className="truncate font-mono text-text-muted" title={key}>
              {key}
            </dt>
            <dd className="numeric ml-auto shrink-0">
              {value === null ? (
                <span className="text-text-muted">unavailable</span>
              ) : (
                String(value)
              )}
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
