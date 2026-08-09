import ProvenanceBadge, { type Provenance } from "@/components/ProvenanceBadge";
import { SEVERITY_TONE, ruleLabel, type AlertRecord } from "@/components/alerts";

/**
 * Open alerts, most severe first.
 *
 * Two things are deliberate. Each alert shows the figures behind it, so it can
 * be checked rather than believed. And a stateful alert — one describing a
 * condition rather than a moment — says that it will close itself, because
 * offering a dismiss button for "collection has stopped" invites dismissing a
 * problem that is still happening.
 */
export default function AlertPanel({
  alerts,
  acknowledge,
  emptyMessage = "Nothing needs your attention.",
}: {
  alerts: AlertRecord[];
  acknowledge?: (alert: AlertRecord) => React.ReactNode;
  emptyMessage?: string;
}) {
  if (alerts.length === 0) {
    return (
      <p className="rounded-xl border border-dashed border-border bg-surface/50 p-5 text-sm text-text-muted">
        {emptyMessage}
      </p>
    );
  }

  const order = { CRITICAL: 0, WARNING: 1, INFO: 2 } as const;
  const sorted = [...alerts].sort((a, b) => order[a.severity] - order[b.severity]);

  return (
    <div className="space-y-3">
      {sorted.map((alert) => (
        <article
          key={alert.id}
          className={`rounded-xl border p-5 ${SEVERITY_TONE[alert.severity] ?? SEVERITY_TONE.INFO}`}
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded border border-current/30 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide">
              {alert.severity.toLowerCase()}
            </span>
            <span className="text-xs uppercase tracking-wide opacity-70">
              {ruleLabel(alert.rule_key)}
            </span>
            <ProvenanceBadge provenance={alert.provenance as Provenance} />
            <span className="ml-auto text-xs opacity-70">
              {new Date(alert.fired_at).toLocaleString()}
            </span>
          </div>

          <h3 className="mt-2 text-sm font-medium text-text">{alert.title}</h3>
          <p className="mt-2 whitespace-pre-line text-sm leading-relaxed text-text-muted">
            {alert.body}
          </p>

          {Object.keys(alert.facts).length > 0 && (
            <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs text-text-muted">
              {Object.entries(alert.facts).map(([key, value]) => (
                <div key={key} className="flex gap-1.5">
                  <dt className="font-mono">{key}</dt>
                  <dd className="numeric text-text">
                    {value === null ? "unavailable" : String(value)}
                  </dd>
                </div>
              ))}
            </dl>
          )}

          <div className="mt-3 flex flex-wrap items-center gap-3">
            {acknowledge?.(alert)}
            {alert.is_stateful && (
              <p className="text-xs text-text-muted">
                This describes an ongoing condition. It closes itself when the
                condition clears — acknowledging only marks it as seen.
              </p>
            )}
          </div>
        </article>
      ))}
    </div>
  );
}
