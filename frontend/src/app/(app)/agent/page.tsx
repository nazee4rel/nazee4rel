import PhaseNotice from "@/components/PhaseNotice";
import { getAccountsStatus } from "@/lib/api";

export default async function AgentPage() {
  const status = await getAccountsStatus();

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

      <PhaseNotice phase={6} title="Run history">
        Each loop — observe, collect, analyze, reason, recommend, act, verify,
        report — is recorded as a durable run, so a crash resumes rather than
        restarts and you can see exactly what happened at every stage.
      </PhaseNotice>

      <PhaseNotice phase={6} title="Prompt-injection monitoring">
        Replies and quote-posts on your content are text strangers wrote, and the
        agent reads it. Reasoning and acting are therefore separate processes:
        the model holds no tools and returns structured JSON that a deterministic
        executor validates against an allowlist. Any draft produced from a run
        that ingested third-party text is flagged here, and suspected injection
        attempts are surfaced rather than quietly dropped.
      </PhaseNotice>
    </div>
  );
}
