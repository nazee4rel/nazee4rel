import PhaseNotice from "@/components/PhaseNotice";
import ProvenanceBadge from "@/components/ProvenanceBadge";

export default function AudiencePage() {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Audience</h1>
        <p className="mt-1 text-sm text-text-muted">
          Follower growth trends and the audience analytics that are genuinely available.
        </p>
      </header>

      <PhaseNotice phase={4} title="Follower growth">
        Follower count is snapshotted hourly. X exposes only a current count —
        there is no history endpoint — so this series begins the day collection
        starts and cannot be backfilled.
      </PhaseNotice>

      <PhaseNotice phase={5} title="Follower attribution">
        <span className="inline-flex items-center gap-2">
          Which posts drove growth <ProvenanceBadge provenance="INFERRED" />
        </span>
        <p className="mt-2">
          X provides no follower-event stream — nothing records who followed you
          or from where. So this is modelled, not measured: hourly follower
          deltas are regressed against each post&apos;s decay curve to apportion
          growth above baseline. Every figure carries a confidence interval,
          which widens when posts overlap in time, and the model reports that it
          cannot separate contributions rather than inventing a winner.
        </p>
      </PhaseNotice>

      <div className="rounded-xl border border-border bg-surface p-6">
        <div className="flex items-center gap-3">
          <h3 className="text-sm font-medium">Demographics</h3>
          <ProvenanceBadge provenance="UNAVAILABLE" />
        </div>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-text-muted">
          Age, gender, location and interest breakdowns are{" "}
          <strong className="text-text">not available</strong> through the X API.
          X removed audience analytics in 2020; estimated demographics exist only
          in Ads Manager, behind a separate ad account and a separate API
          approval.
        </p>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-text-muted">
          This panel will keep saying so rather than filling the gap with
          guesses. If you obtain Ads API access, an optional integration can be
          added — clearly labelled as estimated, because X infers those
          attributes rather than collecting them.
        </p>
      </div>
    </div>
  );
}
