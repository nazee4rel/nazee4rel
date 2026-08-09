import Caveats from "@/components/Caveats";
import ProvenanceBadge from "@/components/ProvenanceBadge";
import { BarSeries, ChartCard, type Point } from "@/components/charts";
import { money, type RevenueResponse } from "@/components/analytics";
import { apiFetch, getAccountsStatus } from "@/lib/api";

import RevenueImport from "./RevenueImport";

export default async function RevenuePage() {
  const status = await getAccountsStatus();
  const account = status?.accounts[0];

  if (!account) {
    return (
      <div className="rounded-xl border border-dashed border-border bg-surface/50 p-6 text-sm text-text-muted">
        Connect an X account before recording revenue against it.
      </div>
    );
  }

  const result = await apiFetch<RevenueResponse>(`/api/v1/analytics/${account.id}/revenue`);
  const revenue = result.ok ? result.data : null;

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Revenue</h1>
        <p className="mt-1 text-sm text-text-muted">
          Revenue by source, monthly totals, campaign revenue and per-post RPM.
        </p>
      </header>

      <div className="rounded-xl border border-warning/30 bg-warning/5 p-5">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="text-sm font-medium">Where this data comes from</h2>
          <ProvenanceBadge provenance="IMPORTED" />
          <ProvenanceBadge provenance="USER_ENTERED" />
        </div>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-text-muted">
          X publishes <strong className="text-text">no API</strong> for creator
          earnings — Ads Revenue Share, Subscriptions and Tips all lack developer
          endpoints. Every figure below comes from you. Nothing is estimated on
          your behalf.
        </p>
      </div>

      <RevenueImport accountId={account.id} />

      {revenue && revenue.entry_count > 0 ? (
        <>
          <section className="grid gap-4 sm:grid-cols-3">
            <Stat
              label={`Total (${revenue.currency})`}
              value={money(revenue.total_minor, revenue.currency)}
              note={`${revenue.entry_count} entries recorded`}
            />
            <Stat
              label="Month over month"
              value={
                revenue.growth_ratio === null
                  ? "—"
                  : `${revenue.growth_ratio >= 0 ? "+" : ""}${(revenue.growth_ratio * 100).toFixed(0)}%`
              }
              note={
                revenue.growth_ratio === null
                  ? "Needs two months with recorded revenue"
                  : "Latest month against the one before"
              }
            />
            <Stat
              label="Best source"
              value={revenue.best_source?.replace(/_/g, " ").toLowerCase() ?? "—"}
              note="By total recorded amount"
            />
          </section>

          {revenue.by_source.length > 0 && (
            <section className="space-y-3">
              <h2 className="text-sm font-medium">By source</h2>
              <div className="space-y-2 rounded-xl border border-border bg-surface p-5">
                {revenue.by_source.map((source) => (
                  <div key={source.source_type} className="flex items-center gap-3">
                    <span className="w-40 shrink-0 truncate text-xs text-text-muted">
                      {source.source_type.replace(/_/g, " ").toLowerCase()}
                    </span>
                    <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface-raised">
                      <div
                        className="h-full rounded-full bg-accent"
                        style={{ width: `${Math.max(2, source.share * 100)}%` }}
                      />
                    </div>
                    <span className="numeric w-24 shrink-0 text-right text-xs">
                      {money(source.total_minor, revenue.currency)}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          )}

          {revenue.by_month.length > 1 && (
            <ChartCard
              title="Recorded revenue by month"
              subtitle="Every bar is a figure you entered or imported — none of it came from X"
              readout={<ProvenanceBadge provenance="USER_ENTERED" />}
              startLabel={revenue.by_month[0]?.month}
              endLabel={revenue.by_month[revenue.by_month.length - 1]?.month}
              footer={
                <>
                  Months with no recorded entries are absent from this chart rather than
                  drawn at zero. A month you simply have not imported yet is not a month
                  you earned nothing.
                </>
              }
            >
              <BarSeries
                points={revenue.by_month.map(
                  (month): Point => ({
                    label: month.month,
                    value: month.total_minor / 100,
                    note: `${month.entry_count} entr${month.entry_count === 1 ? "y" : "ies"}`,
                  }),
                )}
                formatValue={(v) => money(Math.round(v * 100), revenue.currency)}
                label="Revenue by month"
              />
            </ChartCard>
          )}

          {revenue.by_month.length > 0 && (
            <section className="space-y-3">
              <h2 className="text-sm font-medium">By month</h2>
              <div className="overflow-x-auto rounded-xl border border-border bg-surface">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-text-muted">
                      <th className="px-4 py-3 font-medium">Month</th>
                      <th className="px-4 py-3 text-right font-medium">Entries</th>
                      <th className="px-4 py-3 text-right font-medium">Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {revenue.by_month.map((month) => (
                      <tr key={month.month} className="border-b border-border/50 last:border-0">
                        <td className="numeric px-4 py-3">{month.month}</td>
                        <td className="numeric px-4 py-3 text-right text-text-muted">
                          {month.entry_count}
                        </td>
                        <td className="numeric px-4 py-3 text-right">
                          {money(month.total_minor, revenue.currency)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {revenue.per_post.length > 0 && (
            <section className="space-y-3">
              <h2 className="text-sm font-medium">Per post, with RPM</h2>
              <div className="overflow-x-auto rounded-xl border border-border bg-surface">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-text-muted">
                      <th className="px-4 py-3 font-medium">Post</th>
                      <th className="px-4 py-3 text-right font-medium">Revenue</th>
                      <th className="px-4 py-3 text-right font-medium">Impressions</th>
                      <th className="px-4 py-3 text-right font-medium">RPM</th>
                    </tr>
                  </thead>
                  <tbody>
                    {revenue.per_post.map((post) => (
                      <tr key={post.post_id} className="border-b border-border/50 last:border-0">
                        <td className="px-4 py-3 font-mono text-xs text-text-muted">
                          {post.post_id.slice(0, 8)}
                        </td>
                        <td className="numeric px-4 py-3 text-right">
                          {money(post.revenue_minor, revenue.currency)}
                        </td>
                        <td className="numeric px-4 py-3 text-right text-text-muted">
                          {post.impressions?.toLocaleString() ?? "—"}
                        </td>
                        <td className="numeric px-4 py-3 text-right">
                          {post.rpm_available ? (
                            money(Math.round(post.rpm_minor ?? 0), revenue.currency)
                          ) : (
                            // Suppressed, not approximated: a confidently wrong
                            // RPM invites decisions.
                            <span title={post.reason_unavailable ?? undefined}>
                              <ProvenanceBadge provenance="UNAVAILABLE" />
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-xs leading-relaxed text-text-muted">
                RPM is revenue per 1,000 impressions. It is shown only where both
                halves exist — where impressions were never collected, it is marked
                unavailable rather than computed against a partial denominator.
              </p>
            </section>
          )}

          <Caveats items={revenue.caveats} />
        </>
      ) : (
        <div className="rounded-xl border border-dashed border-border bg-surface/50 p-6 text-sm text-text-muted">
          No revenue recorded yet. Import a CSV above to get started.
        </div>
      )}
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
