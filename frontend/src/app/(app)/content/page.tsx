import Caveats, { BucketBar } from "@/components/Caveats";
import ProvenanceBadge from "@/components/ProvenanceBadge";
import RangeTabs, { parseDays } from "@/components/RangeTabs";
import {
  formatRate,
  type FormatsResponse,
  type PostRow,
  type PostsResponse,
  type TimingResponse,
  type TopicsResponse,
} from "@/components/analytics";
import { apiFetch, getAccountsStatus } from "@/lib/api";

const WINDOWS = [7, 30, 90];

export default async function ContentPage({
  searchParams,
}: {
  searchParams: Promise<{ days?: string }>;
}) {
  const { days: rawDays } = await searchParams;
  const days = parseDays(rawDays, 30, WINDOWS);

  const status = await getAccountsStatus();
  const account = status?.accounts[0];

  if (!account) {
    return <EmptyState message="Connect an X account to see content analytics." />;
  }

  // Timing and format analysis need a longer run-up than the selected window to
  // have enough posts per bucket, so they are fetched over a wider period and
  // say so rather than being silently restricted to it.
  const patternDays = Math.max(days, 90);

  const [postsResult, timingResult, formatsResult, topicsResult] = await Promise.all([
    apiFetch<PostsResponse>(`/api/v1/analytics/${account.id}/posts?days=${days}`),
    apiFetch<TimingResponse>(`/api/v1/analytics/${account.id}/timing?days=${patternDays}`),
    apiFetch<FormatsResponse>(`/api/v1/analytics/${account.id}/formats?days=${patternDays}`),
    apiFetch<TopicsResponse>(`/api/v1/analytics/${account.id}/topics?days=${patternDays}`),
  ]);

  const posts = postsResult.ok ? postsResult.data : null;
  const timing = timingResult.ok ? timingResult.data : null;
  const formats = formatsResult.ok ? formatsResult.data : null;
  const topics = topicsResult.ok ? topicsResult.data : null;

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Content Analytics</h1>
          <p className="mt-1 text-sm text-text-muted">
            Top posts, topic and format performance, and posting-time analysis.
          </p>
        </div>
        <RangeTabs basePath="/content" current={days} options={WINDOWS} />
      </header>

      {posts && posts.total_posts === 0 ? (
        <EmptyState
          message={`No posts collected in the last ${days} days. Collection runs on a schedule — this page fills in as posts are discovered and snapshotted.`}
        />
      ) : (
        posts && (
          <>
            <section className="space-y-3">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="text-sm font-medium">Best performing</h2>
                <p className="text-xs text-text-muted">
                  Ranked by engagement rate across {posts.comparable_posts} comparable posts
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

      {/* ----------------------------------------------------------- topics */}
      {topics && (
        <section className="space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-sm font-medium">Topic performance</h2>
            <ProvenanceBadge provenance="INFERRED" />
            {topics.best && (
              <p className="text-xs text-text-muted">
                Best: <span className="text-positive">{topics.best}</span>
                {topics.worst && topics.worst !== topics.best && (
                  <>
                    {" · "}Weakest: <span className="text-warning">{topics.worst}</span>
                  </>
                )}
              </p>
            )}
          </div>

          <p className="max-w-3xl text-xs leading-relaxed text-text-muted">
            Topics are assigned by the agent from a taxonomy you control, so unlike the
            formats below they carry classification error — that is what the{" "}
            <em>modelled</em> badge means. The taxonomy is deliberately closed: free-form
            labels drift between runs, and the moment they do, comparing topics across
            periods stops meaning anything.
            {topics.classified_posts > 0 && (
              <>
                {" "}
                Covering {topics.classified_posts} of {topics.total_posts} posts in the last{" "}
                {patternDays} days.
              </>
            )}
          </p>

          {topics.topics.length > 0 ? (
            <div className="space-y-2 rounded-xl border border-border bg-surface p-5">
              {topics.topics.map((topic) => (
                <BucketBar
                  key={topic.topic}
                  label={topic.topic}
                  value={topic.median_engagement_rate ?? 0}
                  max={Math.max(
                    ...topics.topics.map((t) => t.median_engagement_rate ?? 0),
                    0.0001,
                  )}
                  sampleSize={topic.posts}
                  isReliable={topic.is_reliable}
                  status={topic.status}
                />
              ))}
            </div>
          ) : (
            <div className="rounded-xl border border-dashed border-border bg-surface/50 p-5 text-sm text-text-muted">
              No posts have been categorised yet. Classification runs as part of the agent
              cycle — start one from the AI Insights page.
            </div>
          )}
          <Caveats items={topics.caveats} />
        </section>
      )}

      {/* ---------------------------------------------------------- formats */}
      {formats && (
        <section className="space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-sm font-medium">Format performance</h2>
            <ProvenanceBadge provenance="DERIVED" />
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
          <p className="max-w-3xl text-xs leading-relaxed text-text-muted">
            Media, links, threads and length are detected mechanically at collection time,
            so these groupings need no model and carry no classification error.
          </p>
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

      {/* ----------------------------------------------------------- timing */}
      {timing && (
        <section className="space-y-3">
          <h2 className="text-sm font-medium">Posting time</h2>
          <div className="rounded-xl border border-border bg-surface p-5">
            <p className="text-sm leading-relaxed">{timing.recommendation}</p>
            <p className="mt-1 text-xs text-text-muted">
              Hours shown in {timing.timezone}, your local time — a recommendation in UTC
              would be useless. Based on {timing.total_posts} posts over {patternDays} days.
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
