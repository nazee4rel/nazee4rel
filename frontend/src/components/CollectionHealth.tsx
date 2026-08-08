/**
 * Is data actually being collected?
 *
 * This panel exists because silent failure is the expensive outcome in this
 * system. A stopped collector looks exactly like a quiet account until you go
 * looking for a month of impressions that no longer exist. So the state is
 * shown prominently rather than left to be inferred from an empty chart.
 */

export type CollectionHealthData = {
  collecting: boolean;
  last_account_snapshot_at: string | null;
  hours_since_account_snapshot: number | null;
  posts_tracked: number;
  posts_in_window: number;
  posts_awaiting_freeze: number;
  posts_frozen: number;
  posts_impressions_never_available: number;
  total_snapshots: number;
  warnings: string[];
};

function Stat({
  label,
  value,
  hint,
  tone = "normal",
}: {
  label: string;
  value: number | string;
  hint?: string;
  tone?: "normal" | "warning" | "muted";
}) {
  const valueClass = {
    normal: "text-text",
    warning: "text-warning",
    muted: "text-text-muted",
  }[tone];

  return (
    <div className="rounded-lg border border-border bg-surface-raised p-3">
      <p className="text-xs uppercase tracking-wide text-text-muted">{label}</p>
      <p className={`numeric mt-1 text-lg font-semibold ${valueClass}`}>{value}</p>
      {hint && <p className="mt-1 text-xs leading-relaxed text-text-muted">{hint}</p>}
    </div>
  );
}

export default function CollectionHealth({ health }: { health: CollectionHealthData }) {
  return (
    <section className="rounded-xl border border-border bg-surface p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-medium">Collection</h2>
        <span
          className={`inline-flex items-center gap-1.5 text-xs ${
            health.collecting ? "text-positive" : "text-negative"
          }`}
        >
          <span
            aria-hidden
            className={`inline-block h-1.5 w-1.5 rounded-full ${
              health.collecting ? "bg-positive" : "bg-negative"
            }`}
          />
          {health.collecting
            ? `Active — last snapshot ${health.hours_since_account_snapshot?.toFixed(1)}h ago`
            : "Not collecting"}
        </span>
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Posts tracked"
          value={health.posts_tracked}
          hint={`${health.posts_in_window} still inside the 30-day metrics window`}
        />
        <Stat
          label="Snapshots"
          value={health.total_snapshots}
          hint="Append-only history — the permanent record"
        />
        <Stat
          label="Frozen"
          value={health.posts_frozen}
          hint="Impressions captured before their window closed"
        />
        <Stat
          label="Awaiting freeze"
          value={health.posts_awaiting_freeze}
          tone={health.posts_awaiting_freeze > 0 ? "warning" : "normal"}
          hint="In their final 24 hours before impressions expire"
        />
      </div>

      {health.posts_impressions_never_available > 0 && (
        <p className="mt-4 text-xs leading-relaxed text-text-muted">
          <strong className="text-text">
            {health.posts_impressions_never_available} post
            {health.posts_impressions_never_available === 1 ? "" : "s"}
          </strong>{" "}
          were already past 30 days when collection began, so X never returned
          their impressions. Those are shown as unavailable rather than as zero —
          they are not a collection failure, and no amount of retrying can
          recover them.
        </p>
      )}

      {health.warnings.map((warning) => (
        <div
          key={warning}
          role="alert"
          className="mt-4 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm leading-relaxed text-warning"
        >
          {warning}
        </div>
      ))}
    </section>
  );
}
