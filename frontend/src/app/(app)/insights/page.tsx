import PhaseNotice from "@/components/PhaseNotice";

export default function InsightsPage() {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">AI Insights</h1>
        <p className="mt-1 text-sm text-text-muted">
          What is working, what is not, why performance changed, and what to do next.
        </p>
      </header>

      <PhaseNotice phase={6} title="Reasoning over collected data">
        The analytics engine computes the numbers; the model interprets them and
        never calculates them itself. Every claim it makes carries the figures it
        was derived from, so an assertion can be checked against the data rather
        than taken on trust.
      </PhaseNotice>

      <PhaseNotice phase={6} title="Recommendations that get graded">
        Each recommendation is stored as a falsifiable prediction with a check
        date, and a later run marks it confirmed, refuted or inconclusive. Those
        grades feed back into subsequent prompts and are visible here — so you
        can see whether the agent&apos;s advice has actually worked. Without that
        loop, an insights panel is just confident-sounding text.
      </PhaseNotice>
    </div>
  );
}
