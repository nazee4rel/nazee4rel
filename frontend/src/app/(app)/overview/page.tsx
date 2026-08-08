import CapabilityMatrix from "@/components/CapabilityMatrix";
import Caveats from "@/components/Caveats";
import CollectionHealth, { type CollectionHealthData } from "@/components/CollectionHealth";
import MetricTile from "@/components/MetricTile";
import ProvenanceBadge from "@/components/ProvenanceBadge";
import { BarSeries, ChartCard, GapLegend, LineChart, type Point } from "@/components/charts";
import type { AgentAction, AgentRun } from "@/components/agent";
import {
  compact,
  formatRate,
  money,
  shortDay,
  signed,
  type SeriesResponse,
  type SummaryResponse,
} from "@/components/analytics";
import { apiFetch, getAccountsStatus } from "@/lib/api";

import { AccountControls, ConnectXButton } from "./XConnection";

type UsageSummary = {
  budget: {
    state: "healthy" | "warning" | "critical" | "exhausted";
    spent_usd: number;
    budget_usd: number;
    fraction_used: number;
    billing_mode: string;
  };
  blocked_calls: number;
  by_endpoint: { endpoint: string; calls: number; resources: number; cost_usd: number }[];
};

// Statuses X's callback can hand back, phrased for a human rather than echoing
// a raw error code.
const CONNECT_MESSAGES: Record<string, { text: string; tone: "good" | "bad" }> = {
  success: { text: "X account connected. Capabilities were probed automatically.", tone: "good" },
  declined: { text: "Authorization was declined, so nothing was connected.", tone: "bad" },
  failed: {
    text: "The connection could not be completed. The authorization link may have expired — try again.",
    tone: "bad",
  },
  invalid: { text: "That authorization link was invalid. Start the connection again.", tone: "bad" },
};

export default async function OverviewPage({
  searchParams,
}: {
  searchParams: Promise<{ x_connect?: string }>;
}) {
  const { x_connect } = await searchParams;
  const status = await getAccountsStatus();

  if (!status) {
    return (
      <div className="rounded-xl border border-negative/40 bg-negative/10 p-6 text-sm text-negative">
        Could not reach the backend. Check that the stack is running (
        <code className="text-xs">docker compose ps</code>).
      </div>
    );
  }

  const account = status.accounts[0];
  const banner = x_connect ? CONNECT_MESSAGES[x_connect] : undefined;

  // One round trip per concern, issued together. The headline figures all come
  // from `/summary`, so they cannot disagree with each other.
  const [summaryResult, seriesResult, healthResult, usageResult, runsResult, actionsResult] =
    await Promise.all([
      account
        ? apiFetch<SummaryResponse>(`/api/v1/analytics/${account.id}/summary?days=30`)
        : null,
      account ? apiFetch<SeriesResponse>(`/api/v1/analytics/${account.id}/series?days=30`) : null,
      account
        ? apiFetch<CollectionHealthData>(`/api/v1/collection/${account.id}/health`)
        : null,
      apiFetch<UsageSummary>("/api/v1/x/usage"),
      account ? apiFetch<AgentRun[]>(`/api/v1/agent/${account.id}/runs?limit=1`) : null,
      account
        ? apiFetch<AgentAction[]>(`/api/v1/agent/${account.id}/actions?pending_only=true`)
        : null,
    ]);

  const summary = summaryResult?.ok ? summaryResult.data : null;
  const series = seriesResult?.ok ? seriesResult.data : null;
  const health = healthResult?.ok ? healthResult.data : null;
  const usage = usageResult.ok ? usageResult.data : null;
  const latestRun = runsResult?.ok ? (runsResult.data[0] ?? null) : null;
  const pending = actionsResult?.ok ? actionsResult.data : [];

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Overview</h1>
        <p className="mt-1 text-sm text-text-muted">
          {account
            ? `@${account.username} — followers, engagement, impressions and revenue over the last 30 days.`
            : "Followers, engagement rate, impressions, revenue and growth score."}
        </p>
      </header>

      {banner && (
        <div
          role="status"
          className={`rounded-xl border p-4 text-sm ${
            banner.tone === "good"
              ? "border-positive/40 bg-positive/10 text-positive"
              : "border-warning/40 bg-warning/10 text-warning"
          }`}
        >
          {banner.text}
        </div>
      )}

      {!status.connected && (
        <div className="rounded-xl border border-border bg-surface p-6">
          <h2 className="text-sm font-medium">Connect your X account</h2>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-text-muted">
            Collection begins as soon as you connect, and it is worth doing sooner rather
            than later: X only returns impressions for posts under 30 days old. Any post
            older than that when collection starts can never have its impression data
            recovered.
          </p>
          <div className="mt-5">
            <ConnectXButton writeEnabled={status.write_actions_enabled} />
          </div>
        </div>
      )}

      {/* --------------------------------------------------------- headline */}
      {summary && (
        <>
          <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
            <MetricTile
              label="Followers"
              value={summary.followers.value === null ? null : summary.followers.value.toLocaleString()}
              provenance="MEASURED"
              unavailableReason="No follower snapshot recorded yet."
              change={
                summary.followers.change_7d === null
                  ? null
                  : {
                      text: `${signed(summary.followers.change_7d)} / 7d`,
                      positive: summary.followers.change_7d >= 0,
                    }
              }
              note={
                summary.followers.change_note ??
                (summary.followers.baseline_reliable && summary.followers.median_daily_change !== null
                  ? `Typically ${signed(Math.round(summary.followers.median_daily_change))} a day (median)`
                  : `${summary.followers.hours_of_history}h of history collected`)
              }
            />

            <MetricTile
              label="Engagement rate"
              value={
                summary.engagement.median_rate === null
                  ? null
                  : `${(summary.engagement.median_rate * 100).toFixed(2)}%`
              }
              provenance="DERIVED"
              unavailableReason="No post has a usable denominator yet."
              note={
                summary.engagement.median_rate === null
                  ? undefined
                  : `Median across ${summary.engagement.sample_size} posts, per ${
                      summary.engagement.basis === "IMPRESSIONS"
                        ? "impression"
                        : summary.engagement.basis === "FOLLOWERS"
                          ? "follower — impressions unavailable"
                          : "a mix of denominators"
                    }`
              }
            />

            <MetricTile
              label="Impressions"
              value={
                summary.impressions.total === null ? null : compact(summary.impressions.total)
              }
              provenance="MEASURED"
              unavailableReason="Impressions were never collected for these posts. X stops returning them after 30 days."
              note={
                summary.impressions.total === null
                  ? undefined
                  : `Across ${summary.impressions.posts_counted} post(s)` +
                    (summary.impressions.posts_missing > 0
                      ? ` · ${summary.impressions.posts_missing} excluded, not counted as zero`
                      : "")
              }
            />

            <MetricTile
              label="Revenue"
              value={
                summary.revenue.entry_count === 0
                  ? null
                  : money(summary.revenue.total_minor, summary.revenue.currency)
              }
              provenance="USER_ENTERED"
              unavailableReason="Nothing recorded. X publishes no creator-earnings API, so this comes from you."
              note={`${summary.revenue.entry_count} entr${summary.revenue.entry_count === 1 ? "y" : "ies"} — imported or entered by you, never measured`}
            />

            <MetricTile
              label="Growth score"
              value={
                summary.growth_score.score === null
                  ? null
                  : summary.growth_score.score.toFixed(0)
              }
              provenance="DERIVED"
              unavailableReason={summary.growth_score.explanation}
              note={
                summary.growth_score.score === null
                  ? undefined
                  : Object.entries(summary.growth_score.components)
                      .map(([key, value]) => `${key.replace(/_/g, " ")} ${value.toFixed(0)}`)
                      .join(" · ")
              }
            />
          </section>

          {summary.latest_anomaly && summary.latest_anomaly.direction !== "NORMAL" && (
            <div
              className={`rounded-xl border px-4 py-3 text-sm ${
                summary.latest_anomaly.direction === "SPIKE"
                  ? "border-positive/40 bg-positive/10 text-positive"
                  : "border-warning/40 bg-warning/10 text-warning"
              }`}
            >
              <strong className="capitalize">
                {summary.latest_anomaly.direction.toLowerCase()}
              </strong>{" "}
              — {summary.latest_anomaly.explanation}
            </div>
          )}
        </>
      )}

      {/* ----------------------------------------------------------- charts */}
      {series && series.daily.length > 1 && (
        <div className="grid gap-4 xl:grid-cols-2">
          <ChartCard
            title="Followers"
            subtitle="Hourly snapshots — this series is the only follower history that exists"
            readout={
              <div className="flex items-center gap-2">
                <ProvenanceBadge provenance="MEASURED" />
              </div>
            }
            startLabel={series.daily[0] ? shortDay(series.daily[0].day) : undefined}
            endLabel={
              series.daily.length > 0
                ? shortDay(series.daily[series.daily.length - 1].day)
                : undefined
            }
            footer={series.gaps.length > 0 ? <GapLegend /> : undefined}
          >
            <LineChart
              points={series.daily.map(
                (d): Point => ({ label: shortDay(d.day), value: d.followers })
              )}
              label="Follower count over time"
            />
          </ChartCard>

          <ChartCard
            title="Daily change"
            subtitle="Gained or lost each day, with posting days marked"
            readout={<ProvenanceBadge provenance="DERIVED" />}
            startLabel={series.daily[0] ? shortDay(series.daily[0].day) : undefined}
            endLabel={
              series.daily.length > 0
                ? shortDay(series.daily[series.daily.length - 1].day)
                : undefined
            }
            footer={
              <>
                A day with no snapshot is hatched, and the day after a break carries no
                figure at all — two days of growth landing on one day would invent a spike
                that never happened.
              </>
            }
          >
            <BarSeries
              points={series.daily.map(
                (d): Point => ({
                  label: shortDay(d.day),
                  value: d.delta,
                  note: d.posts > 0 ? `${d.posts} post(s)` : undefined,
                })
              )}
              formatValue={(v) => signed(v)}
              label="Daily follower change"
            />
          </ChartCard>
        </div>
      )}

      {series && <Caveats items={series.caveats} />}

      {/* ------------------------------------------------- agent + approvals */}
      {account && (latestRun || pending.length > 0) && (
        <section className="grid gap-4 xl:grid-cols-2">
          {latestRun && (
            <div className="rounded-xl border border-border bg-surface p-5">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="text-sm font-medium">Latest agent brief</h2>
                <span className="text-xs text-text-muted">
                  {new Date(latestRun.started_at).toLocaleString()} ·{" "}
                  {latestRun.status.toLowerCase()}
                </span>
              </div>
              <p className="mt-3 whitespace-pre-line text-sm leading-relaxed text-text-muted">
                {latestRun.summary ?? "No summary recorded for this run."}
              </p>
              <a
                href="/insights"
                className="mt-3 inline-block text-xs text-accent hover:underline"
              >
                See the insights and recommendations →
              </a>
            </div>
          )}

          {pending.length > 0 && (
            <div className="rounded-xl border border-accent/30 bg-accent/5 p-5">
              <h2 className="text-sm font-medium">
                {pending.length} action{pending.length === 1 ? "" : "s"} waiting for you
              </h2>
              <ul className="mt-3 space-y-2 text-sm text-text-muted">
                {pending.slice(0, 4).map((action) => (
                  <li key={action.id}>
                    <span className="text-text">{action.summary}</span>
                    <span className="ml-2 text-xs">({action.autonomy_tier})</span>
                  </li>
                ))}
              </ul>
              <a href="/agent" className="mt-3 inline-block text-xs text-accent hover:underline">
                Review the approval queue →
              </a>
            </div>
          )}
        </section>
      )}

      {/* --------------------------------------------------------- top post */}
      {summary?.top_post && (
        <section className="rounded-xl border border-border bg-surface p-5">
          <h2 className="text-sm font-medium">Best post in this window</h2>
          <p className="mt-2 text-sm leading-relaxed">{summary.top_post.text || "(no text)"}</p>
          <div className="mt-3 flex flex-wrap gap-4 text-xs text-text-muted">
            <span>{new Date(summary.top_post.posted_at).toLocaleString()}</span>
            <span className="numeric">{summary.top_post.engagement} engagements</span>
            <span className="numeric">
              {summary.top_post.impressions === null
                ? "impressions unavailable"
                : `${summary.top_post.impressions.toLocaleString()} impressions`}
            </span>
            <span className="numeric" title={summary.top_post.engagement_rate.description}>
              {formatRate(summary.top_post.engagement_rate)} engagement rate
            </span>
          </div>
        </section>
      )}

      {summary && <Caveats items={summary.caveats} />}

      {/* ------------------------------------------------------- operations */}
      <details className="rounded-xl border border-border bg-surface" open={!status.connected}>
        <summary className="cursor-pointer px-5 py-4 text-sm font-medium">
          Collection, spend and API access
        </summary>
        <div className="space-y-6 border-t border-border p-5">
          {status.accounts.map((connected) => (
            <section key={connected.id} className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-medium">@{connected.username}</h3>
                  <p className="mt-0.5 text-xs text-text-muted">
                    {connected.collection_started_at
                      ? `Collecting since ${new Date(
                          connected.collection_started_at,
                        ).toLocaleDateString()} — nothing before this date can be backfilled`
                      : "Collection not started"}
                  </p>
                </div>
                <AccountControls accountId={connected.id} username={connected.username} />
              </div>
              <CapabilityMatrix capabilities={connected.capabilities} />
            </section>
          ))}

          {health && <CollectionHealth health={health} />}
          {usage && <ApiSpend usage={usage} />}

          <div className="grid gap-4 sm:grid-cols-2">
            <ConfigCard
              label="API billing mode"
              value={status.billing_mode.replace(/_/g, "-")}
              note="Reading your own data bills at the reduced Owned Reads rate."
            />
            <ConfigCard
              label="Agent posting"
              value={status.write_actions_enabled ? "Enabled" : "Disabled"}
              note={
                status.write_actions_enabled
                  ? "Drafts require your explicit per-post approval."
                  : "No write scope is requested, so the agent cannot publish."
              }
            />
          </div>
        </div>
      </details>
    </div>
  );
}

function ApiSpend({ usage }: { usage: UsageSummary }) {
  const { budget } = usage;
  const tone = {
    healthy: "text-positive",
    warning: "text-warning",
    critical: "text-warning",
    exhausted: "text-negative",
  }[budget.state];

  return (
    <section className="rounded-xl border border-border bg-surface-raised/40 p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium">X API spend this month</h3>
        <p className={`numeric text-sm ${tone}`}>
          ${budget.spent_usd.toFixed(3)} of ${budget.budget_usd.toFixed(2)}
        </p>
      </div>

      <div
        className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-surface-raised"
        role="progressbar"
        aria-valuenow={Math.round(budget.fraction_used * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className={`h-full rounded-full ${
            budget.state === "exhausted"
              ? "bg-negative"
              : budget.state === "healthy"
                ? "bg-positive"
                : "bg-warning"
          }`}
          style={{ width: `${Math.min(100, budget.fraction_used * 100)}%` }}
        />
      </div>

      {usage.by_endpoint.length > 0 ? (
        <table className="mt-4 w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-text-muted">
              <th className="pb-2 font-medium">Endpoint</th>
              <th className="pb-2 text-right font-medium">Calls</th>
              <th className="pb-2 text-right font-medium">Resources</th>
              <th className="pb-2 text-right font-medium">Cost</th>
            </tr>
          </thead>
          <tbody>
            {usage.by_endpoint.map((row) => (
              <tr key={row.endpoint} className="border-t border-border/50">
                <td className="py-2 text-text-muted">{row.endpoint}</td>
                <td className="numeric py-2 text-right">{row.calls}</td>
                <td className="numeric py-2 text-right">{row.resources}</td>
                <td className="numeric py-2 text-right">${row.cost_usd.toFixed(4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="mt-3 text-xs text-text-muted">No API calls recorded this month.</p>
      )}

      {usage.blocked_calls > 0 && (
        <p className="mt-3 text-xs text-warning">
          {usage.blocked_calls} call{usage.blocked_calls === 1 ? "" : "s"} blocked (budget,
          rate limit or unavailable capability). Blocked calls cost nothing but explain gaps
          in collection.
        </p>
      )}
    </section>
  );
}

function ConfigCard({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="rounded-xl border border-border bg-surface-raised/40 p-4">
      <p className="text-xs uppercase tracking-wide text-text-muted">{label}</p>
      <p className="numeric mt-1.5 text-lg font-semibold capitalize">{value}</p>
      <p className="mt-1.5 text-xs leading-relaxed text-text-muted">{note}</p>
    </div>
  );
}
