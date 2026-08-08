import Caveats from "@/components/Caveats";
import ProvenanceBadge from "@/components/ProvenanceBadge";
import type { AttributionResponse } from "@/components/analytics";
import { apiFetch, getAccountsStatus } from "@/lib/api";

type OverviewResponse = {
  followers: number | null;
  hours_of_history: number;
  growth_baseline: {
    median_daily: number;
    observations: number;
    is_reliable: boolean;
    note: string;
  } | null;
  latest_anomaly: {
    direction: string;
    severity: string;
    z_score: number | null;
    explanation: string;
  } | null;
  trend: {
    direction: string;
    change_ratio: number | null;
    explanation: string;
    is_reliable: boolean;
  } | null;
  daily_deltas: [string, number][];
  caveats: string[];
};

export default async function AudiencePage() {
  const status = await getAccountsStatus();
  const account = status?.accounts[0];

  if (!account) {
    return (
      <div className="rounded-xl border border-dashed border-border bg-surface/50 p-6 text-sm text-text-muted">
        Connect an X account to see audience analytics.
      </div>
    );
  }

  const [overviewResult, attributionResult] = await Promise.all([
    apiFetch<OverviewResponse>(`/api/v1/analytics/${account.id}/overview`),
    apiFetch<AttributionResponse>(`/api/v1/analytics/${account.id}/attribution`),
  ]);

  const overview = overviewResult.ok ? overviewResult.data : null;
  const attribution = attributionResult.ok ? attributionResult.data : null;

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Audience</h1>
        <p className="mt-1 text-sm text-text-muted">
          Follower growth, what drove it, and the audience data that genuinely exists.
        </p>
      </header>

      {overview && (
        <section className="space-y-3">
          <h2 className="text-sm font-medium">Follower growth</h2>
          <div className="grid gap-4 sm:grid-cols-3">
            <Stat
              label="Followers"
              value={overview.followers?.toLocaleString() ?? "—"}
              note={`${overview.hours_of_history}h of history collected`}
            />
            <Stat
              label="Typical daily change"
              value={
                overview.growth_baseline?.is_reliable
                  ? `${overview.growth_baseline.median_daily >= 0 ? "+" : ""}${overview.growth_baseline.median_daily.toFixed(0)}`
                  : "—"
              }
              note={
                overview.growth_baseline?.is_reliable
                  ? "Median, not mean — one viral day should not redefine 'normal'"
                  : (overview.growth_baseline?.note ?? "Not enough history yet")
              }
            />
            <Stat
              label="Trend"
              value={overview.trend?.is_reliable ? overview.trend.direction : "—"}
              note={overview.trend?.explanation ?? "Needs two comparable periods"}
            />
          </div>

          {overview.latest_anomaly && overview.latest_anomaly.direction !== "NORMAL" && (
            <div
              className={`rounded-lg border px-4 py-3 text-sm ${
                overview.latest_anomaly.direction === "SPIKE"
                  ? "border-positive/40 bg-positive/10 text-positive"
                  : "border-warning/40 bg-warning/10 text-warning"
              }`}
            >
              <strong className="capitalize">
                {overview.latest_anomaly.direction.toLowerCase()} detected
              </strong>{" "}
              — {overview.latest_anomaly.explanation}
            </div>
          )}

          <Caveats items={overview.caveats} />
        </section>
      )}

      {attribution && (
        <section className="space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-sm font-medium">Which posts drove growth</h2>
            <ProvenanceBadge provenance="INFERRED" />
            <span className="text-xs text-text-muted">
              confidence: {attribution.confidence.toLowerCase()}
            </span>
          </div>

          <p className="max-w-3xl text-sm leading-relaxed text-text-muted">
            {attribution.summary}
          </p>

          {attribution.is_usable && attribution.contributions.length > 0 ? (
            <div className="overflow-x-auto rounded-xl border border-border bg-surface">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-text-muted">
                    <th className="px-4 py-3 font-medium">Posted</th>
                    <th className="px-4 py-3 text-right font-medium">Estimate</th>
                    <th className="px-4 py-3 text-right font-medium">90% interval</th>
                    <th className="px-4 py-3 font-medium">Reading</th>
                  </tr>
                </thead>
                <tbody>
                  {attribution.contributions.slice(0, 10).map((c) => (
                    <tr key={c.post_id} className="border-b border-border/50 last:border-0">
                      <td className="px-4 py-3 text-xs text-text-muted">
                        {new Date(c.posted_at).toLocaleString()}
                      </td>
                      <td className="numeric px-4 py-3 text-right">
                        {c.estimate.toFixed(0)}
                      </td>
                      <td className="numeric px-4 py-3 text-right text-text-muted">
                        {c.ci_low.toFixed(0)} – {c.ci_high.toFixed(0)}
                      </td>
                      <td className="px-4 py-3 text-xs">
                        {c.is_distinguishable ? (
                          <span className="text-inferred">
                            distinguishable effect
                          </span>
                        ) : (
                          // Deliberately not "no effect" — the data cannot show one.
                          <span className="text-text-muted">
                            cannot be shown to have had an effect
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="rounded-xl border border-dashed border-border bg-surface/50 p-5 text-sm text-text-muted">
              Attribution is not available yet — it needs several days of hourly
              follower history and at least two posts inside that window.
            </div>
          )}

          <Caveats items={attribution.caveats} tone="warning" />
        </section>
      )}

      <section className="rounded-xl border border-border bg-surface p-6">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-medium">Demographics</h2>
          <ProvenanceBadge provenance="UNAVAILABLE" />
        </div>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-text-muted">
          Age, gender, location and interest breakdowns are{" "}
          <strong className="text-text">not available</strong> through the X API. X
          removed audience analytics in 2020; estimated demographics exist only in
          Ads Manager, behind a separate ad account and a separate API approval.
        </p>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-text-muted">
          This panel will keep saying so rather than filling the gap with guesses.
        </p>
      </section>
    </div>
  );
}

function Stat({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="rounded-xl border border-border bg-surface p-4">
      <p className="text-xs uppercase tracking-wide text-text-muted">{label}</p>
      <p className="numeric mt-1.5 text-2xl font-semibold capitalize">{value}</p>
      <p className="mt-1.5 text-xs leading-relaxed text-text-muted">{note}</p>
    </div>
  );
}
