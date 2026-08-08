import type { Capability, CapabilityStatus } from "@/lib/api";

/**
 * The capability matrix from §1.5 of the architecture doc.
 *
 * X's access rules change often and are documented inconsistently, so the
 * system probes rather than assumes — and shows the reader the result. Note
 * that UNKNOWN ("not yet checked") is rendered distinctly from UNAVAILABLE
 * ("confirmed absent"); collapsing the two would let the dashboard imply
 * knowledge it does not have.
 */

const STATUS: Record<CapabilityStatus, { label: string; className: string }> = {
  AVAILABLE: { label: "Available", className: "text-positive" },
  UNAVAILABLE: { label: "Unavailable", className: "text-text-muted" },
  FORBIDDEN: { label: "Forbidden", className: "text-negative" },
  ERROR: { label: "Probe failed", className: "text-warning" },
  UNKNOWN: { label: "Not yet checked", className: "text-text-muted/60" },
};

const DESCRIPTIONS: Record<string, string> = {
  READ_OWN_PROFILE: "Follower count and profile fields",
  READ_OWN_POSTS: "Your posts and their text",
  READ_PUBLIC_METRICS: "Likes, replies, reposts, bookmarks",
  READ_NON_PUBLIC_METRICS: "Impressions, link clicks, profile clicks — posts under 30 days only",
  READ_ORGANIC_METRICS: "Organic engagement breakdown — posts under 30 days only",
  READ_FOLLOWERS_LIST: "Follower list — removed from some access tiers",
  READ_LIKED_POSTS: "Posts you have liked",
  READ_BOOKMARKS: "Your bookmarks",
  WRITE_POSTS: "Publish posts — only with per-draft approval",
};

export default function CapabilityMatrix({ capabilities }: { capabilities: Capability[] }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-border bg-surface">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-text-muted">
            <th className="px-4 py-3 font-medium">Capability</th>
            <th className="px-4 py-3 font-medium">Status</th>
            <th className="px-4 py-3 font-medium">Last checked</th>
          </tr>
        </thead>
        <tbody>
          {capabilities.map((cap) => {
            const status = STATUS[cap.status];
            return (
              <tr key={cap.capability} className="border-b border-border/50 last:border-0">
                <td className="px-4 py-3">
                  <div className="font-medium text-text">
                    {cap.capability.replace(/_/g, " ").toLowerCase()}
                  </div>
                  <div className="mt-0.5 text-xs text-text-muted">
                    {DESCRIPTIONS[cap.capability] ?? ""}
                  </div>
                </td>
                <td className={`px-4 py-3 whitespace-nowrap ${status.className}`}>
                  {status.label}
                  {cap.last_error && (
                    <div className="mt-0.5 text-xs text-text-muted">{cap.last_error}</div>
                  )}
                </td>
                <td className="numeric px-4 py-3 whitespace-nowrap text-xs text-text-muted">
                  {cap.last_checked_at
                    ? new Date(cap.last_checked_at).toLocaleString()
                    : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
