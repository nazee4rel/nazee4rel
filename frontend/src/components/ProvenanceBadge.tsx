/**
 * Renders the provenance of a value.
 *
 * Section 2 of the architecture doc: every metric declares where it came from,
 * and that declaration is visible to the reader rather than buried in the
 * schema. A modelled follower-attribution number must never be presented with
 * the same visual authority as a figure X actually reported.
 *
 * Used from Phase 5 onward; defined now so no later component invents its own
 * inconsistent treatment.
 */

export type Provenance =
  | "MEASURED"
  | "DERIVED"
  | "INFERRED"
  | "USER_ENTERED"
  | "IMPORTED"
  | "UNAVAILABLE";

const STYLES: Record<Provenance, { label: string; className: string; title: string }> = {
  MEASURED: {
    label: "Measured",
    className: "bg-positive/10 text-positive border-positive/30",
    title: "Reported directly by the X API.",
  },
  DERIVED: {
    label: "Derived",
    className: "bg-accent/10 text-accent border-accent/30",
    title: "Calculated arithmetically from measured values.",
  },
  INFERRED: {
    label: "Modelled",
    className: "bg-inferred/10 text-inferred border-inferred/30",
    title:
      "A statistical estimate, not a measurement. X provides no follower-attribution data, so this is inferred from timing correlations and carries a confidence interval.",
  },
  USER_ENTERED: {
    label: "Entered by you",
    className: "bg-warning/10 text-warning border-warning/30",
    title: "Manually recorded. X provides no API for this data.",
  },
  IMPORTED: {
    label: "Imported",
    className: "bg-warning/10 text-warning border-warning/30",
    title: "Imported from a CSV or a connected third-party account.",
  },
  UNAVAILABLE: {
    label: "Unavailable",
    className: "bg-surface-raised text-text-muted border-border",
    title:
      "Not obtainable at your X API access level. Shown as unavailable rather than as zero.",
  },
};

export default function ProvenanceBadge({ provenance }: { provenance: Provenance }) {
  const style = STYLES[provenance];
  return (
    <span
      title={style.title}
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${style.className}`}
    >
      {style.label}
    </span>
  );
}
