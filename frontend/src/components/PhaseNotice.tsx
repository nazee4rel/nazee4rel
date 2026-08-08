/**
 * Placeholder for a dashboard section whose data arrives in a later phase.
 *
 * Deliberately explicit about *why* a panel is empty. An analytics dashboard
 * that renders a zeroed chart when it simply has no data yet is the exact
 * failure this project is built to avoid — "no data yet" and "the value is
 * zero" are different facts and must never look alike.
 */
export default function PhaseNotice({
  phase,
  title,
  children,
}: {
  phase: number;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-dashed border-border bg-surface/50 p-6">
      <div className="flex items-center gap-3">
        <span className="rounded-md bg-surface-raised px-2 py-1 text-xs font-medium text-accent">
          Phase {phase}
        </span>
        <h3 className="text-sm font-medium text-text">{title}</h3>
      </div>
      {children && <div className="mt-3 text-sm leading-relaxed text-text-muted">{children}</div>}
    </div>
  );
}
