import Link from "next/link";

/**
 * Window selector, rendered as links rather than as a client-side control.
 *
 * The whole dashboard is Server Components, and a range change is a new query
 * against the database — there is nothing to hold in client state. Links keep
 * the selection in the URL, so a particular view is shareable and survives a
 * refresh, and the page ships no JavaScript for it.
 */
export default function RangeTabs({
  basePath,
  current,
  options = [7, 30, 90],
}: {
  basePath: string;
  current: number;
  options?: number[];
}) {
  return (
    <nav aria-label="Time window" className="flex items-center gap-1 text-xs">
      {options.map((days) => {
        const active = days === current;
        return (
          <Link
            key={days}
            href={`${basePath}?days=${days}`}
            aria-current={active ? "page" : undefined}
            className={`rounded-lg border px-2.5 py-1 transition ${
              active
                ? "border-accent/50 bg-accent/10 text-accent"
                : "border-border text-text-muted hover:bg-surface-raised"
            }`}
          >
            {days}d
          </Link>
        );
      })}
    </nav>
  );
}

/** Clamp a `days` query parameter to something the API will accept. */
export function parseDays(raw: string | undefined, fallback: number, allowed: number[]): number {
  const value = Number(raw);
  return allowed.includes(value) ? value : fallback;
}
