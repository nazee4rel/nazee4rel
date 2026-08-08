import PhaseNotice from "@/components/PhaseNotice";
import ProvenanceBadge from "@/components/ProvenanceBadge";

export default function RevenuePage() {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Revenue</h1>
        <p className="mt-1 text-sm text-text-muted">
          Revenue by source, monthly totals, campaign revenue and trends.
        </p>
      </header>

      <div className="rounded-xl border border-warning/30 bg-warning/5 p-6">
        <div className="flex items-center gap-3">
          <h3 className="text-sm font-medium">Where this data comes from</h3>
          <ProvenanceBadge provenance="IMPORTED" />
          <ProvenanceBadge provenance="USER_ENTERED" />
        </div>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-text-muted">
          X publishes <strong className="text-text">no API</strong> for creator
          earnings — Ads Revenue Share, Subscriptions and Tips all lack developer
          endpoints. Every figure in this section therefore comes from you, and
          is labelled accordingly. Nothing here is estimated on your behalf.
        </p>
      </div>

      <PhaseNotice phase={5} title="CSV import">
        Bulk import of payout statements with column mapping and duplicate
        detection — your chosen primary path. A small entry form ships alongside
        it, since imported rows sometimes need correcting and sponsorship or
        brand-deal income has no statement to import.
      </PhaseNotice>

      <PhaseNotice phase={5} title="Revenue per 1,000 impressions">
        Computable only for periods where both revenue and impressions exist.
        Where impressions were never collected, the metric is suppressed rather
        than approximated from a partial denominator.
      </PhaseNotice>
    </div>
  );
}
