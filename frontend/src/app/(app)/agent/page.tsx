import PhaseNotice from "@/components/PhaseNotice";
import { apiFetch, getAccountsStatus } from "@/lib/api";

type Run = {
  id: string;
  kind: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  posts_discovered: number;
  snapshots_written: number;
  skipped_count: number;
  degraded_reason: string | null;
  error: string | null;
};

const STATUS_TONE: Record<string, string> = {
  SUCCEEDED: "text-positive",
  PARTIAL: "text-warning",
  SKIPPED: "text-text-muted",
  FAILED: "text-negative",
  RUNNING: "text-accent",
};

export default async function AgentPage() {
  const status = await getAccountsStatus();
  const account = status?.accounts[0];

  const runsResult = account
    ? await apiFetch<Run[]>(`/api/v1/collection/${account.id}/runs?limit=25`)
    : null;
  const runs = runsResult?.ok ? runsResult.data : [];

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Agent Activity</h1>
        <p className="mt-1 text-sm text-text-muted">
          What the agent observed, analyzed, recommended and did — plus errors and warnings.
        </p>
      </header>

      <section className="rounded-xl border border-border bg-surface p-6">
        <h2 className="text-sm font-medium">Autonomy</h2>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-text-muted">
          Actions are tiered, and the tier is enforced in code rather than by
          prompting. Collection and analysis run automatically; posting requires
          your approval of that specific draft; anything that spends money needs
          explicit confirmation; and deleting posts, following, unfollowing and
          DMs are not implemented at all.
        </p>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-text-muted">
          Posting is currently{" "}
          <strong className="text-text">
            {status?.write_actions_enabled ? "enabled" : "disabled"}
          </strong>
          .{" "}
          {status?.write_actions_enabled
            ? "Drafts appear here for per-post approval and expire after 24 hours."
            : "No write scope is requested, so the agent cannot publish even if every other control failed."}
        </p>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-medium">Recent collection runs</h2>
        {runs.length === 0 ? (
          <p className="rounded-xl border border-dashed border-border bg-surface/50 p-6 text-sm text-text-muted">
            No collection runs recorded yet. Connect an X account and run a cycle
            from the Overview page, or wait for the scheduler to fire.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-border bg-surface">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-text-muted">
                  <th className="px-4 py-3 font-medium">When</th>
                  <th className="px-4 py-3 font-medium">Job</th>
                  <th className="px-4 py-3 font-medium">Result</th>
                  <th className="px-4 py-3 text-right font-medium">Found</th>
                  <th className="px-4 py-3 text-right font-medium">Snapshots</th>
                  <th className="px-4 py-3 text-right font-medium">Skipped</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id} className="border-b border-border/50 last:border-0">
                    <td className="numeric px-4 py-3 whitespace-nowrap text-xs text-text-muted">
                      {new Date(run.started_at).toLocaleString()}
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap">
                      {run.kind.replace(/_/g, " ").toLowerCase()}
                      {run.kind === "FINAL_FREEZE" && (
                        <span
                          className="ml-2 rounded bg-accent/10 px-1.5 py-0.5 text-[10px] uppercase text-accent"
                          title="Captured impressions before the 30-day window closed. This data cannot be re-fetched afterwards."
                        >
                          pre-cliff
                        </span>
                      )}
                    </td>
                    <td className={`px-4 py-3 whitespace-nowrap ${STATUS_TONE[run.status] ?? ""}`}>
                      {run.status.toLowerCase()}
                      {run.degraded_reason && (
                        <div className="text-xs text-warning">{run.degraded_reason}</div>
                      )}
                      {run.error && (
                        <div className="max-w-xs truncate text-xs text-negative" title={run.error}>
                          {run.error}
                        </div>
                      )}
                    </td>
                    <td className="numeric px-4 py-3 text-right">{run.posts_discovered}</td>
                    <td className="numeric px-4 py-3 text-right">{run.snapshots_written}</td>
                    <td
                      className={`numeric px-4 py-3 text-right ${
                        run.skipped_count > 0 ? "text-warning" : ""
                      }`}
                    >
                      {run.skipped_count}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <PhaseNotice phase={6} title="Reasoning, recommendations and verification">
        Collection is the observe step. From Phase&nbsp;6 each loop also reasons
        over the data, produces recommendations as falsifiable predictions, and
        grades them later — so you can see whether the advice actually worked.
      </PhaseNotice>

      <PhaseNotice phase={6} title="Prompt-injection monitoring">
        Replies and quote-posts on your content are text strangers wrote, and the
        agent reads it. Reasoning and acting stay separate processes: the model
        holds no tools and returns structured JSON that a deterministic executor
        validates against an allowlist.
      </PhaseNotice>
    </div>
  );
}
