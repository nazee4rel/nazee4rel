import CapabilityMatrix from "@/components/CapabilityMatrix";
import PhaseNotice from "@/components/PhaseNotice";
import { getAccountsStatus } from "@/lib/api";

export default async function OverviewPage() {
  const status = await getAccountsStatus();

  if (!status) {
    return (
      <div className="rounded-xl border border-negative/40 bg-negative/10 p-6 text-sm text-negative">
        Could not reach the backend. Check that the stack is running
        (<code className="text-xs">docker compose ps</code>).
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Overview</h1>
        <p className="mt-1 text-sm text-text-muted">
          Followers, engagement, impressions, revenue and growth score.
        </p>
      </header>

      {!status.connected ? (
        <div className="rounded-xl border border-border bg-surface p-6">
          <h2 className="text-sm font-medium">No X account connected</h2>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-text-muted">
            The OAuth&nbsp;2.0 connect flow arrives in Phase&nbsp;3. Once connected,
            data collection begins immediately — and it is worth starting sooner
            rather than later: X only returns impressions for posts under 30 days
            old, so any post older than that when collection begins can never have
            its impression data recovered.
          </p>
        </div>
      ) : (
        <div className="space-y-6">
          {status.accounts.map((account) => (
            <section key={account.id} className="space-y-3">
              <div className="flex items-baseline gap-3">
                <h2 className="text-sm font-medium">@{account.username}</h2>
                <span className="text-xs text-text-muted">
                  {account.collection_started_at
                    ? `Collecting since ${new Date(
                        account.collection_started_at,
                      ).toLocaleDateString()}`
                    : "Collection not started"}
                </span>
              </div>
              <CapabilityMatrix capabilities={account.capabilities} />
            </section>
          ))}
        </div>
      )}

      <section className="grid gap-4 sm:grid-cols-3">
        <ConfigCard
          label="API billing mode"
          value={status.billing_mode.replace(/_/g, "-")}
          note="Owned Reads for your own data are billed at the reduced rate."
        />
        <ConfigCard
          label="Monthly API budget"
          value={`$${status.monthly_budget_usd.toFixed(2)}`}
          note="The collector degrades its schedule rather than exceeding this."
        />
        <ConfigCard
          label="Agent posting"
          value={status.write_actions_enabled ? "Enabled" : "Disabled"}
          note={
            status.write_actions_enabled
              ? "Drafts still require your explicit per-post approval."
              : "The agent cannot publish. No write scope is requested."
          }
        />
      </section>

      <PhaseNotice phase={5} title="Metrics arrive with the analytics engine">
        Follower growth, engagement rate, impressions and growth score need
        collected history to compute. Collection starts in Phase&nbsp;4.
      </PhaseNotice>
    </div>
  );
}

function ConfigCard({
  label,
  value,
  note,
}: {
  label: string;
  value: string;
  note: string;
}) {
  return (
    <div className="rounded-xl border border-border bg-surface p-4">
      <p className="text-xs uppercase tracking-wide text-text-muted">{label}</p>
      <p className="numeric mt-1.5 text-lg font-semibold capitalize">{value}</p>
      <p className="mt-1.5 text-xs leading-relaxed text-text-muted">{note}</p>
    </div>
  );
}
