import Caveats from "@/components/Caveats";
import ProvenanceBadge from "@/components/ProvenanceBadge";
import RangeTabs, { parseDays } from "@/components/RangeTabs";
import { ChartCard, GapLegend, LineChart, type Point } from "@/components/charts";
import {
  shortDay,
  signed,
  type AttributionResponse,
  type SeasonalityProfile,
  type SeasonalityResponse,
  type SeriesResponse,
  type SummaryResponse,
} from "@/components/analytics";
import { apiFetch, getAccountsStatus } from "@/lib/api";

const WINDOWS = [30, 90, 180];

export default async function AudiencePage({
  searchParams,
}: {
  searchParams: Promise<{ days?: string }>;
}) {
  const { days: rawDays } = await searchParams;
  const days = parseDays(rawDays, 30, WINDOWS);

  const status = await getAccountsStatus();
  const account = status?.accounts[0];

  if (!account) {
    return (
      <div className="rounded-xl border border-dashed border-border bg-surface/50 p-6 text-sm text-text-muted">
        Connect an X account to see audience analytics.
      </div>
    );
  }

  const [summaryResult, seriesResult, attributionResult, seasonalityResult] = await Promise.all([
    apiFetch<SummaryResponse>(`/api/v1/analytics/${account.id}/summary?days=${days}`),
    apiFetch<SeriesResponse>(`/api/v1/analytics/${account.id}/series?days=${days}`),
    // The attribution model is capped at 90 days: beyond that the design
    // matrix has more posts than the follower series can separate.
    apiFetch<AttributionResponse>(
      `/api/v1/analytics/${account.id}/attribution?days=${Math.min(days, 90)}`,
    ),
    apiFetch<SeasonalityResponse>(
      `/api/v1/analytics/${account.id}/seasonality?days=${Math.max(days, 90)}`,
    ),
  ]);

  const summary = summaryResult.ok ? summaryResult.data : null;
  const series = seriesResult.ok ? seriesResult.data : null;
  const attribution = attributionResult.ok ? attributionResult.data : null;
  const seasonality = seasonalityResult.ok ? seasonalityResult.data : null;

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Audience</h1>
          <p className="mt-1 text-sm text-text-muted">
            Follower growth, what drove it, and the audience data that genuinely exists.
          </p>
        </div>
        <RangeTabs basePath="/audience" current={days} options={WINDOWS} />
      </header>

      {/* ---------------------------------------------------- follower growth */}
      {summary && (
        <section className="space-y-3">
          <h2 className="text-sm font-medium">Follower growth</h2>
          <div className="grid gap-4 sm:grid-cols-3">
            <Stat
              label="Followers"
              value={summary.followers.value?.toLocaleString() ?? "—"}
              note={`${summary.followers.hours_of_history}h of history collected`}
            />
            <Stat
              label="Typical daily change"
              value={
                summary.followers.baseline_reliable &&
                summary.followers.median_daily_change !== null
                  ? signed(Math.round(summary.followers.median_daily_change))
                  : "—"
              }
              note={
                summary.followers.baseline_reliable
                  ? "Median, not mean — one viral day should not redefine 'normal'"
                  : "Not enough history for a reliable baseline yet"
              }
            />
            <Stat
              label="Trend"
              value={summary.trend?.is_reliable ? summary.trend.direction : "—"}
              note={summary.trend?.explanation ?? "Needs two comparable periods"}
            />
          </div>

          {summary.latest_anomaly && summary.latest_anomaly.direction !== "NORMAL" && (
            <div
              className={`rounded-lg border px-4 py-3 text-sm ${
                summary.latest_anomaly.direction === "SPIKE"
                  ? "border-positive/40 bg-positive/10 text-positive"
                  : "border-warning/40 bg-warning/10 text-warning"
              }`}
            >
              <strong className="capitalize">
                {summary.latest_anomaly.direction.toLowerCase()} detected
              </strong>{" "}
              — {summary.latest_anomaly.explanation}
            </div>
          )}
        </section>
      )}

      {series && series.daily.length > 1 && (
        <ChartCard
          title="Follower history"
          subtitle="X has no follower-history endpoint — this series exists only because it was collected"
          readout={<ProvenanceBadge provenance="MEASURED" />}
          startLabel={shortDay(series.daily[0].day)}
          endLabel={shortDay(series.daily[series.daily.length - 1].day)}
          footer={series.gaps.length > 0 ? <GapLegend /> : undefined}
        >
          <LineChart
            points={series.daily.map((d): Point => ({ label: shortDay(d.day), value: d.followers }))}
            height={220}
            label="Follower count over time"
          />
        </ChartCard>
      )}

      {series && <Caveats items={series.caveats} />}

      {/* ------------------------------------------------------- attribution */}
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
                      <td className="numeric px-4 py-3 text-right">{c.estimate.toFixed(0)}</td>
                      <td className="numeric px-4 py-3 text-right text-text-muted">
                        {c.ci_low.toFixed(0)} – {c.ci_high.toFixed(0)}
                      </td>
                      <td className="px-4 py-3 text-xs">
                        {c.is_distinguishable ? (
                          <span className="text-inferred">distinguishable effect</span>
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
              Attribution is not available yet — it needs several days of hourly follower
              history and at least two posts inside that window.
            </div>
          )}

          <Caveats items={attribution.caveats} tone="warning" />
        </section>
      )}

      {/* ------------------------------------------------------- seasonality */}
      {seasonality && (
        <section className="space-y-3">
          <h2 className="text-sm font-medium">Weekday rhythm</h2>
          <p className="max-w-3xl text-xs leading-relaxed text-text-muted">
            Two separate profiles, because they answer different questions: which days you
            gain followers, and which days your posts land. They are often not the same day,
            and a combined figure would hide that. A weekday with fewer than{" "}
            {seasonality.minimum_per_weekday} observations has no median at all — it is left
            blank rather than filled in.
          </p>

          <div className="grid gap-4 xl:grid-cols-2">
            <WeekdayProfile
              title="Followers gained"
              profile={seasonality.follower_change}
              format={(v) => signed(Math.round(v))}
              observed={`${seasonality.follower_days_observed} day(s) observed`}
            />
            <WeekdayProfile
              title="Engagement rate"
              profile={seasonality.engagement_rate}
              format={(v) => `${(v * 100).toFixed(2)}%`}
              observed={`${seasonality.posts_observed} post(s) observed`}
            />
          </div>

          <Caveats items={seasonality.caveats} />
        </section>
      )}

      {/* ------------------------------------------------------ demographics */}
      <section className="rounded-xl border border-border bg-surface p-6">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-medium">Demographics</h2>
          <ProvenanceBadge provenance="UNAVAILABLE" />
        </div>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-text-muted">
          Age, gender, location and interest breakdowns are{" "}
          <strong className="text-text">not available</strong> through the X API. X removed
          audience analytics in 2020; estimated demographics exist only in Ads Manager,
          behind a separate ad account and a separate API approval.
        </p>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-text-muted">
          This panel will keep saying so rather than filling the gap with guesses.
        </p>
      </section>
    </div>
  );
}

/**
 * Weekday medians as a bar row.
 *
 * A weekday with too few observations renders as an empty slot with its reason,
 * not as a zero-height bar — those look identical to a genuinely bad day.
 */
function WeekdayProfile({
  title,
  profile,
  format,
  observed,
}: {
  title: string;
  profile: SeasonalityProfile;
  format: (value: number) => string;
  observed: string;
}) {
  const values = profile.days.map((d) => d.median).filter((v): v is number => v !== null);
  const max = values.length > 0 ? Math.max(...values.map(Math.abs), 0.0001) : 1;

  return (
    <div className="rounded-xl border border-border bg-surface p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium">{title}</h3>
        <span className={`text-xs ${profile.is_reliable ? "text-text-muted" : "text-warning"}`}>
          {profile.is_reliable ? observed : "not enough data to read a rhythm"}
        </span>
      </div>

      <div className="mt-4 space-y-2">
        {profile.days.map((day) => (
          <div key={day.weekday} className="flex items-center gap-3">
            <span className="w-24 shrink-0 text-xs text-text-muted">{day.name}</span>
            <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface-raised">
              {day.median !== null && (
                <div
                  className={`h-full rounded-full ${
                    day.median >= 0 ? "bg-accent" : "bg-negative"
                  }`}
                  style={{ width: `${Math.max(2, (Math.abs(day.median) / max) * 100)}%` }}
                />
              )}
            </div>
            <span
              className={`numeric w-20 shrink-0 text-right text-xs ${
                day.median === null ? "text-text-muted/60" : ""
              }`}
              title={
                day.median === null
                  ? `${day.observations} observation(s) — too few for a median`
                  : `${day.observations} observation(s)`
              }
            >
              {day.median === null ? "—" : format(day.median)}
            </span>
          </div>
        ))}
      </div>
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
