import ProvenanceBadge, { type Provenance } from "@/components/ProvenanceBadge";

/**
 * One headline figure.
 *
 * `value` is nullable on purpose. A tile with nothing behind it renders the
 * UNAVAILABLE badge and its reason, rather than a dash that reads as "zero" at
 * a glance or a 0 that reads as a measurement. This is the component most
 * likely to be reused for a metric that does not exist yet, so the absent case
 * is the one it handles best.
 */
export default function MetricTile({
  label,
  value,
  provenance,
  note,
  change,
  unavailableReason,
}: {
  label: string;
  value: string | null;
  provenance: Provenance;
  note?: string;
  /** A signed secondary figure, e.g. the seven-day change. */
  change?: { text: string; positive: boolean } | null;
  unavailableReason?: string;
}) {
  return (
    <div className="rounded-xl border border-border bg-surface p-4">
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs uppercase tracking-wide text-text-muted">{label}</p>
        <ProvenanceBadge provenance={value === null ? "UNAVAILABLE" : provenance} />
      </div>

      {value === null ? (
        <p className="mt-2 text-sm leading-relaxed text-text-muted">
          {unavailableReason ?? "Not collected yet."}
        </p>
      ) : (
        <div className="mt-1.5 flex items-baseline gap-2">
          <p className="numeric text-2xl font-semibold">{value}</p>
          {change && (
            <span
              className={`numeric text-xs ${change.positive ? "text-positive" : "text-negative"}`}
            >
              {change.text}
            </span>
          )}
        </div>
      )}

      {note && <p className="mt-1.5 text-xs leading-relaxed text-text-muted">{note}</p>}
    </div>
  );
}
