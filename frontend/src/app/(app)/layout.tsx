import { redirect } from "next/navigation";

import { getCurrentUser } from "@/lib/api";

import NavLink from "./NavLink";
import { logout } from "../login/actions";

// The six sections from the architecture doc. Sections whose data arrives in a
// later phase are still routable now, so the shell is navigable end-to-end and
// each phase has somewhere to land.
const SECTIONS = [
  { href: "/overview", label: "Overview", phase: 7 },
  { href: "/content", label: "Content Analytics", phase: 5 },
  { href: "/audience", label: "Audience", phase: 5 },
  { href: "/revenue", label: "Revenue", phase: 5 },
  { href: "/insights", label: "AI Insights", phase: 6 },
  { href: "/agent", label: "Agent Activity", phase: 6 },
];

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const user = await getCurrentUser();
  if (!user) redirect("/login");

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-surface">
        <div className="border-b border-border px-5 py-4">
          <p className="text-sm font-semibold tracking-tight">X Intelligence</p>
          <p className="mt-0.5 text-xs text-text-muted">Phase 2 · foundation</p>
        </div>

        <nav className="flex-1 space-y-0.5 p-3">
          {SECTIONS.map((section) => (
            <NavLink key={section.href} href={section.href} label={section.label} />
          ))}
        </nav>

        <div className="border-t border-border p-3">
          <p className="truncate px-2 text-xs text-text-muted" title={user.email}>
            {user.display_name}
          </p>
          <p className="truncate px-2 text-[11px] text-text-muted/70">{user.timezone}</p>
          <form action={logout} className="mt-2">
            <button
              type="submit"
              className="w-full rounded-md px-2 py-1.5 text-left text-xs text-text-muted transition hover:bg-surface-raised hover:text-text"
            >
              Sign out
            </button>
          </form>
        </div>
      </aside>

      <main className="flex-1 overflow-x-auto px-8 py-7">{children}</main>
    </div>
  );
}
