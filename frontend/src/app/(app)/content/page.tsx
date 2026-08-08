import Caveats, { BucketBar } from "@/components/Caveats";
import PhaseNotice from "@/components/PhaseNotice";
import ProvenanceBadge from "@/components/ProvenanceBadge";
import {
  formatRate,
  type FormatsResponse,
  type PostRow,
  type PostsResponse,
  type TimingResponse,
} from "@/components/analytics";
import { apiFetch, getAccountsStatus } from "@/lib/api";

export default async function ContentPage() {
  const status = await getAccountsStatus();
  const account = status?.accounts[0];

  if (!account) {
    return (
      <EmptyState message="Connect an X account to see content analytics." />
    );
  }

  const [postsResult, timingResult, formatsResult] = await Promise.all([
    apiFetch<PostsResponse>(`/api/v1/analytics/${account.id}/posts?days=30`),
    apiFetch<TimingResponse>(`/api/v1/analytics/${account.id}/timing`),
    apiFetch<FormatsResponse>(`/api/v1/analytics/${account.id}/formats`),
  ]);

  const posts = postsResult.ok ? postsResult.data : null;
  const timing = timingResult.ok ? timingResult.data : null;
  const formats = formatsResult.ok ? formatsResult.data : null;

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Content Analytics</h1>
        <p className="mt-1 text-sm text-text-muted">
          Top posts, engagement comparison, format performance and posting-time analysis.
        </p>
      </header>

      {posts && posts.total_posts === 0 ? (
        <PhaseNotice phase={4} title="No posts collected yet">
          Collection runs on a schedule. Once posts are discovered and snapshotted,
          this page fills in.
        </PhaseNotice>
      ) : (
        posts && (
          <>
            <section className="space-y-3">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="text-sm font-medium">Best performing</h2>
                <p className="text-xs text-text-muted">
                  Ranked by engagement rate across {posts.comparable_posts} comparable
                  posts
                  {posts.excluded_no_impressions > 0 &&
                    ` · ${posts.excluded_no_impressions} excluded for having no impressions`}
                </p>
              </div>
              <PostTable rows={posts.top} />
            </section>

            {posts.bottom.length > 0 && (
              <section className="space-y-3">
                <h2 className="text-sm font-medium">Weakest performing</h2>
                <PostTable rows={posts.bottom} />
              </section>
            )}
          </>
        )
      )}

      {formats && (
        <section className="space-y-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="text-sm font-medium">Format performance</h2>
            {formats.best && (
              <p className="text-xs text-text-muted">
                Best: <span className="text-positive">{formats.best}</span>
                {formats.worst && (
                  <>
                    {" · "}Weakest: <span className="text-warning">{formats.worst}</span>
                  </>
                )}
              </p>
            )}
          </div>
          {formats.groups.length > 0 ? (
            <div className="space-y-2 rounded-xl border border-border bg-surface p-5">
              {formats.groups.map((group) => (
                <BucketBar
                  key={group.label}
                  label={group.label}
                  value={group.median_performance}
                  max={Math.max(...formats.groups.map((g) => g.median_performance), 0.0001)}
                  sampleSize={group.sample_size}
                  isReliable={group.is_reliable}
                  status={group.status}
                />
              ))}
            </div>
          ) : (
            <p className="text-sm text-text-muted">Not enough posts to compare formats yet.</p>
          )}
          <Caveats items={formats.caveats} />
        </section>
      )}

      {timing && (
        <section className="space-y-3">
          <h2 className="text-sm font-medium">Posting time</h2>
          <div className="rounded-xl border border-border bg-surface p-5">
            <p className="text-sm leading-relaxed">{timing.recommendation}</p>
            <p className="mt-1 text-xs text-text-muted">
              Hours shown in {timing.timezone}, your local time — a recommendation in
              UTC would be useless.
            </p>
            {timing.by_hour.length > 0 && (
              <div className="mt-4 space-y-2">
                {timing.by_hour.map((hour) => (
                  <BucketBar
                    key={hour.label}
                    label={hour.label}
                    value={hour.median_performance}
                    max={Math.max(...timing.by_hour.map((h) => h.median_performance), 0.0001)}
                    sampleSize={hour.sample_size}
                    isReliable={hour.is_reliable}
                    status={hour.status}
                  />
                ))}
              </div>
            )}
          </div>
          <Caveats items={timing.caveats} />
        </section>
      )}

      <PhaseNotice phase={6} title="Topic analysis">
        Topics need classification against a controlled taxonomy, which is the one
        part of the analytics engine that requires a model. It arrives with the
        agent in Phase&nbsp;6 — formats above are detected mechanically and need
        no model at all.
      </PhaseNotice>
    </div>
  );
}

function PostTable({ rows }: { rows: PostRow[] }) {
  if (rows.length === 0) {
    return <p className="text-sm text-text-muted">No comparable posts in this period.</p>;
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-border bg-surface">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-text-muted">
            <th className="px-4 py-3 font-medium">Post</th>
            <th className="px-4 py-3 text-right font-medium">Engagement</th>
            <th className="px-4 py-3 text-right font-medium">Impressions</th>
            <th className="px-4 py-3 text-right font-medium">Rate</th>
            <th className="px-4 py-3 text-right font-medium">Percentile</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.post_id} className="border-b border-border/50 last:border-0">
              <td className="max-w-md px-4 py-3">
                <p className="truncate">{row.text || "(no text)"}</p>
                <p className="mt-0.5 text-xs text-text-muted">
                  {new Date(row.posted_at).toLocaleString()}
                  {row.format.has_media && " · media"}
                  {row.format.has_link && " · link"}
                  {row.format.is_thread && " · thread"}
                </p>
              </td>
              <td className="numeric px-4 py-3 text-right">{row.engagement}</td>
              <td className="numeric px-4 py-3 text-right">
                {row.impressions_available ? (
                  row.impressions?.toLocaleString()
                ) : (
                  // Never a zero here — the value was never obtainable.
                  <span title={row.unavailable_reason ?? undefined}>
                    <ProvenanceBadge provenance="UNAVAILABLE" />
                  </span>
                )}
              </td>
              <td className="numeric px-4 py-3 text-right" title={row.engagement_rate.description}>
                {formatRate(row.engagement_rate)}
                <span className="ml-1 text-[10px] text-text-muted">
                  {row.engagement_rate.denominator === "FOLLOWERS" ? "/foll" : ""}
                </span>
              </td>
              <td className="numeric px-4 py-3 text-right text-text-muted">
                {row.percentile === null ? "—" : `p${row.percentile.toFixed(0)}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="rounded-xl border border-dashed border-border bg-surface/50 p-6 text-sm text-text-muted">
      {message}
    </div>
  );
}
