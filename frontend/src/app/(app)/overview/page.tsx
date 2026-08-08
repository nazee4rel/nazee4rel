import CapabilityMatrix from "@/components/CapabilityMatrix";
import CollectionHealth, { type CollectionHealthData } from "@/components/CollectionHealth";
import PhaseNotice from "@/components/PhaseNotice";
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
  const usageResult = await apiFetch<UsageSummary>("/api/v1/x/usage");
  const usage = usageResult.ok ? usageResult.data : null;

  const firstAccount = status?.accounts[0];
  const healthResult = firstAccount
    ? await apiFetch<CollectionHealthData>(`/api/v1/collection/${firstAccount.id}/health`)
    : null;
  const health = healthResult?.ok ? healthResult.data : null;

  if (!status) {
    return (
      <div className="rounded-xl border border-negative/40 bg-negative/10 p-6 text-sm text-negative">
        Could not reach the backend. Check that the stack is running (
        <code className="text-xs">docker compose ps</code>).
      </div>
    );
  }

  const banner = x_connect ? CONNECT_MESSAGES[x_connect] : undefined;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Overview</h1>
        <p className="mt-1 text-sm text-text-muted">
          Followers, engagement rate, impressions, revenue and growth score.
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

      {!status.connected ? (
        <div className="rounded-xl border border-border bg-surface p-6">
          <h2 className="text-sm font-medium">Connect your X account</h2>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-text-muted">
            Collection begins as soon as you connect, and it is worth doing sooner
            rather than later: X only returns impressions for posts under 30 days
            old. Any post older than that when collection starts can never have
            its impression data recovered.
          </p>
          <div className="mt-5">
            <ConnectXButton writeEnabled={status.write_actions_enabled} />
          </div>
        </div>
      ) : (
        <div className="space-y-6">
          {status.accounts.map((account) => (
            <section key={account.id} className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h2 className="text-sm font-medium">@{account.username}</h2>
                  <p className="mt-0.5 text-xs text-text-muted">
                    {account.collection_started_at
                      ? `Collecting since ${new Date(
                          account.collection_started_at,
                        ).toLocaleDateString()} — nothing before this date can be backfilled`
                      : "Collection not started"}
                  </p>
                </div>
                <AccountControls accountId={account.id} username={account.username} />
              </div>
              <CapabilityMatrix capabilities={account.capabilities} />
            </section>
          ))}
        </div>
      )}

      {health && <CollectionHealth health={health} />}

      {usage && <ApiSpend usage={usage} />}

      <section className="grid gap-4 sm:grid-cols-2">
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
      </section>

      <PhaseNotice phase={5} title="Metrics arrive with the analytics engine">
        Collection is running, so history is accumulating now. Turning it into
        follower growth, engagement rate, topic performance and a growth score
        is Phase&nbsp;5.
      </PhaseNotice>
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
    <section className="rounded-xl border border-border bg-surface p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-medium">X API spend this month</h2>
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
          {usage.blocked_calls} call{usage.blocked_calls === 1 ? "" : "s"} blocked
          (budget, rate limit or unavailable capability). Blocked calls cost
          nothing but explain gaps in collection.
        </p>
      )}
    </section>
  );
}

function ConfigCard({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="rounded-xl border border-border bg-surface p-4">
      <p className="text-xs uppercase tracking-wide text-text-muted">{label}</p>
      <p className="numeric mt-1.5 text-lg font-semibold capitalize">{value}</p>
      <p className="mt-1.5 text-xs leading-relaxed text-text-muted">{note}</p>
    </div>
  );
}
