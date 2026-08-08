import PhaseNotice from "@/components/PhaseNotice";

export default function ContentPage() {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Content Analytics</h1>
        <p className="mt-1 text-sm text-text-muted">
          Top posts, engagement comparison, topic performance, posting-time analysis.
        </p>
      </header>

      <PhaseNotice phase={4} title="Post collection">
        Posts and their metrics are snapshotted on a decaying schedule — hourly
        for the first day, then every six hours, then daily, with a mandatory
        final capture on day&nbsp;29. That last one matters: impressions
        disappear from the X API once a post passes 30 days, so the day-29
        snapshot is the only lasting record.
      </PhaseNotice>

      <PhaseNotice phase={5} title="Topic and format analysis">
        Topic classification uses a controlled, editable taxonomy rather than
        free-form labels, so performance stays comparable across periods. Format
        detection (thread, media, link, length, question) is deterministic and
        turns out to be strongly predictive.
      </PhaseNotice>

      <PhaseNotice phase={5} title="Posting-time analysis">
        Performance by weekday and hour in your local timezone, gated on sample
        size — no recommending 3am Tuesday off the back of two posts.
      </PhaseNotice>
    </div>
  );
}
