/**
 * Renders the caveats an analysis returned.
 *
 * These are shown rather than tucked behind a tooltip on purpose. Most of them
 * describe a limit on what the data can support — "only two posts behind this
 * hour", "these two posts cannot be told apart" — and a reader who misses that
 * will over-trust the number next to it.
 */
export default function Caveats({
  items,
  tone = "muted",
}: {
  items: string[];
  tone?: "muted" | "warning";
}) {
  if (items.length === 0) return null;

  const className =
    tone === "warning"
      ? "border-warning/40 bg-warning/5 text-warning"
      : "border-border bg-surface-raised/50 text-text-muted";

  return (
    <ul className={`space-y-2 rounded-lg border px-4 py-3 text-xs leading-relaxed ${className}`}>
      {items.map((item) => (
        <li key={item} className="flex gap-2">
          <span aria-hidden className="select-none">
            ·
          </span>
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}

/** A labelled bar for comparing buckets, greyed out when under-sampled. */
export function BucketBar({
  label,
  value,
  max,
  sampleSize,
  isReliable,
  status,
}: {
  label: string;
  value: number;
  max: number;
  sampleSize: number;
  isReliable: boolean;
  status: string;
}) {
  const width = max > 0 ? Math.max(2, (value / max) * 100) : 0;

  return (
    <div className="flex items-center gap-3" title={status}>
      <span className="w-36 shrink-0 truncate text-xs text-text-muted">{label}</span>
      <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface-raised">
        <div
          className={`h-full rounded-full ${isReliable ? "bg-accent" : "bg-border"}`}
          style={{ width: `${width}%` }}
        />
      </div>
      <span
        className={`numeric w-20 shrink-0 text-right text-xs ${
          isReliable ? "text-text" : "text-text-muted/60"
        }`}
      >
        {(value * 100).toFixed(2)}%
      </span>
      <span
        className={`numeric w-16 shrink-0 text-right text-xs ${
          isReliable ? "text-text-muted" : "text-warning/70"
        }`}
      >
        n={sampleSize}
      </span>
    </div>
  );
}
