import {
  ACTION_STATUS_TONE,
  RUN_STATUS_TONE,
  type AgentAction,
  type AgentPolicy,
  type AgentRun,
} from "@/components/agent";
import { apiFetch, getAccountsStatus } from "@/lib/api";

import ApprovalCard from "./ApprovalCard";

const STAGES = [
  "OBSERVE",
  "COLLECT",
  "ANALYZE",
  "REASON",
  "RECOMMEND",
  "ACT",
  "VERIFY",
  "REPORT",
];

type CollectionRun = {
  id: string;
  kind: string;
  status: string;
  started_at: string;
  posts_discovered: number;
  snapshots_written: number;
  skipped_count: number;
  degraded_reason: string | null;
  error: string | null;
};

export default async function AgentPage() {
  const status = await getAccountsStatus();
  const account = status?.accounts[0];

  const [policyResult, runsResult, actionsResult, collectionResult] = await Promise.all([
    apiFetch<AgentPolicy>("/api/v1/agent/policy"),
    account
      ? apiFetch<AgentRun[]>(`/api/v1/agent/${account.id}/runs?limit=15`)
      : Promise.resolve(null),
    account
      ? apiFetch<AgentAction[]>(`/api/v1/agent/${account.id}/actions?limit=40`)
      : Promise.resolve(null),
    account
      ? apiFetch<CollectionRun[]>(`/api/v1/collection/${account.id}/runs?limit=15`)
      : Promise.resolve(null),
  ]);

  const policy = policyResult.ok ? policyResult.data : null;
  const runs = runsResult?.ok ? runsResult.data : [];
  const actions = actionsResult?.ok ? actionsResult.data : [];
  const collectionRuns = collectionResult?.ok ? collectionResult.data : [];

  const pending = actions.filter((a) => a.status === "PENDING_APPROVAL");
  const decided = actions.filter((a) => a.status !== "PENDING_APPROVAL");

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Agent Activity</h1>
        <p className="mt-1 text-sm text-text-muted">
          What the agent observed, concluded and did — including everything it was
          refused.
        </p>
      </header>

      {/* ------------------------------------------------------ approval queue */}
      <section className="space-y-3">
        <h2 className="text-sm font-medium">
          Waiting for your approval
          {pending.length > 0 && (
            <span className="ml-2 rounded-full bg-accent px-2 py-0.5 text-[10px] text-canvas">
              {pending.length}
            </span>
          )}
        </h2>
        {pending.length === 0 ? (
          <p className="rounded-xl border border-dashed border-border bg-surface/50 p-6 text-sm text-text-muted">
            Nothing is waiting. Requests expire after 24 hours rather than sitting
            indefinitely — approving one days later would act on analysis that has
            since been superseded.
          </p>
        ) : (
          <div className="space-y-3">
            {pending.map((action) => (
              <ApprovalCard key={action.id} action={action} />
            ))}
          </div>
        )}
      </section>

      {/* -------------------------------------------------------------- policy */}
      <section className="rounded-xl border border-border bg-surface p-5">
        <h2 className="text-sm font-medium">What the agent is allowed to do</h2>
        <p className="mt-2 max-w-3xl text-sm leading-relaxed text-text-muted">
          Read live from the same table the executor enforces, so this cannot drift
          from what is actually permitted. Posting is currently{" "}
          <strong className="text-text">
            {status?.write_actions_enabled ? "enabled" : "disabled"}
          </strong>
          .{" "}
          {status?.write_actions_enabled
            ? "Each draft needs your approval individually."
            : "With it off, the tweet.write scope is never requested — so the stored token could not publish even if every other control failed."}
        </p>

        {policy && (
          <>
            <div className="mt-4 overflow-x-auto rounded-lg border border-border">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-text-muted">
                    <th className="px-4 py-2.5 font-medium">Action</th>
                    <th className="px-4 py-2.5 font-medium">Tier</th>
                    <th className="px-4 py-2.5 font-medium">Effect</th>
                  </tr>
                </thead>
                <tbody>
                  {policy.actions.map((action) => (
                    <tr key={action.action_type} className="border-b border-border/50 last:border-0">
                      <td className="px-4 py-2.5 whitespace-nowrap">
                        {action.action_type.replace(/_/g, " ").toLowerCase()}
                      </td>
                      <td className="px-4 py-2.5 whitespace-nowrap">
                        <span
                          className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${
                            action.tier === "T0"
                              ? "border-border text-text-muted"
                              : "border-warning/40 text-warning"
                          }`}
                        >
                          {action.tier}
                        </span>
                      </td>
                      <td className="px-4 py-2.5 text-xs leading-relaxed text-text-muted">
                        {action.effect}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <p className="mt-3 text-xs leading-relaxed text-text-muted">
              <strong className="text-text">Never implemented:</strong>{" "}
              {policy.never_implemented
                .map((name) => name.replace(/_/g, " ").toLowerCase())
                .join(", ")}
              . These are not disabled settings — there is no code in this system
              that performs them.
            </p>
            <p className="mt-2 text-xs leading-relaxed text-text-muted">{policy.note}</p>
          </>
        )}
      </section>

      {/* ---------------------------------------------------------- agent runs */}
      <section className="space-y-3">
        <h2 className="text-sm font-medium">Agent cycles</h2>
        {runs.length === 0 ? (
          <p className="rounded-xl border border-dashed border-border bg-surface/50 p-6 text-sm text-text-muted">
            No cycles yet. One runs daily; you can also start one from the AI
            Insights page.
          </p>
        ) : (
          <div className="space-y-3">
            {runs.map((run) => (
              <article key={run.id} className="rounded-xl border border-border bg-surface p-5">
                <div className="flex flex-wrap items-center gap-3">
                  <span className={`text-sm font-medium ${RUN_STATUS_TONE[run.status] ?? ""}`}>
                    {run.status.toLowerCase()}
                  </span>
                  <span className="text-xs text-text-muted">
                    {new Date(run.started_at).toLocaleString()} · {run.trigger.toLowerCase()}
                  </span>
                  {run.model && (
                    <span className="text-xs text-text-muted">
                      {run.model} · {run.input_tokens.toLocaleString()} in /{" "}
                      {run.output_tokens.toLocaleString()} out
                      {run.cached_input_tokens > 0 &&
                        ` (${run.cached_input_tokens.toLocaleString()} cached)`}
                    </span>
                  )}
                </div>

                {/* The eight stages, so a stall resolves to a specific one
                    rather than to "the agent". */}
                <ol className="mt-3 flex flex-wrap gap-1.5">
                  {STAGES.map((stage) => {
                    const entry = run.stage_log?.[stage];
                    const ran = entry !== undefined;
                    const ms = typeof entry?.ms === "number" ? entry.ms : null;
                    return (
                      <li
                        key={stage}
                        title={ran ? JSON.stringify(entry, null, 2) : "did not run"}
                        className={`rounded border px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                          ran
                            ? "border-border bg-surface-raised text-text-muted"
                            : "border-dashed border-border/60 text-text-muted/40"
                        }`}
                      >
                        {stage.toLowerCase()}
                        {ms !== null && <span className="numeric ml-1">{ms}ms</span>}
                      </li>
                    );
                  })}
                </ol>

                {run.summary && (
                  <p className="mt-3 text-sm leading-relaxed text-text-muted">{run.summary}</p>
                )}

                <div className="mt-3 flex flex-wrap gap-4 text-xs text-text-muted">
                  <span>{run.insights_created} insight(s)</span>
                  <span>{run.recommendations_created} recommendation(s)</span>
                  <span>{run.actions_created} action(s)</span>
                  {run.grounding_rejections > 0 && (
                    <span
                      className="text-warning"
                      title="Items the model produced that cited figures the evidence did not contain. They were discarded."
                    >
                      {run.grounding_rejections} discarded for bad citations
                    </span>
                  )}
                  {run.ingested_untrusted_content && (
                    <span title="This run read post text — free text this system did not author. Anything it proposed is flagged accordingly.">
                      read unauthored text
                    </span>
                  )}
                </div>

                {run.error && (
                  <p className="mt-2 text-xs text-negative" title={run.error}>
                    {run.error}
                  </p>
                )}
              </article>
            ))}
          </div>
        )}
      </section>

      {/* -------------------------------------------------------- action trail */}
      <section className="space-y-3">
        <h2 className="text-sm font-medium">Action trail</h2>
        <p className="max-w-3xl text-xs leading-relaxed text-text-muted">
          Every action the agent proposed, including the ones that were rejected,
          blocked or expired. The refusals are kept deliberately — they are the more
          informative half of the record.
        </p>
        {decided.length === 0 ? (
          <p className="rounded-xl border border-dashed border-border bg-surface/50 p-6 text-sm text-text-muted">
            No actions recorded yet.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-border bg-surface">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-text-muted">
                  <th className="px-4 py-3 font-medium">When</th>
                  <th className="px-4 py-3 font-medium">Action</th>
                  <th className="px-4 py-3 font-medium">Tier</th>
                  <th className="px-4 py-3 font-medium">Outcome</th>
                </tr>
              </thead>
              <tbody>
                {decided.map((action) => (
                  <tr key={action.id} className="border-b border-border/50 last:border-0">
                    <td className="numeric px-4 py-3 whitespace-nowrap text-xs text-text-muted">
                      {new Date(action.created_at).toLocaleString()}
                    </td>
                    <td className="px-4 py-3">
                      <div>{action.action_type.replace(/_/g, " ").toLowerCase()}</div>
                      <div className="text-xs text-text-muted">{action.summary}</div>
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-xs">{action.autonomy_tier}</td>
                    <td
                      className={`px-4 py-3 text-xs ${ACTION_STATUS_TONE[action.status] ?? ""}`}
                    >
                      {action.status.replace(/_/g, " ").toLowerCase()}
                      {(action.blocked_reason || action.error) && (
                        <div className="mt-0.5 max-w-md text-text-muted">
                          {action.blocked_reason ?? action.error}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ----------------------------------------------------- collection runs */}
      <section className="space-y-3">
        <h2 className="text-sm font-medium">Collection runs</h2>
        <p className="max-w-3xl text-xs leading-relaxed text-text-muted">
          The observe step, running on its own schedule. A gap here is permanent:
          impressions past the 30-day window cannot be re-fetched at any price.
        </p>
        {collectionRuns.length === 0 ? (
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
                {collectionRuns.map((run) => (
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
                    <td
                      className={`px-4 py-3 whitespace-nowrap ${
                        RUN_STATUS_TONE[run.status] ?? (run.status === "SUCCEEDED" ? "text-positive" : "")
                      }`}
                    >
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
    </div>
  );
}
