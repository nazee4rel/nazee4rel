import { redirect } from "next/navigation";

import type { AgentAction } from "@/components/agent";
import { apiFetch, getAccountsStatus, getCurrentUser } from "@/lib/api";

import NavLink from "./NavLink";
import { logout } from "../login/actions";

// The six sections from the architecture doc.
const SECTIONS = [
  { href: "/overview", label: "Overview" },
  { href: "/content", label: "Content Analytics" },
  { href: "/audience", label: "Audience" },
  { href: "/revenue", label: "Revenue" },
  { href: "/insights", label: "AI Insights" },
  { href: "/agent", label: "Agent Activity" },
];

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const user = await getCurrentUser();
  if (!user) redirect("/login");

  // Approval requests expire after 24 hours, so an unanswered one is not
  // harmless — it lapses. The badge exists so the queue is visible from every
  // page rather than only from the one nobody has open.
  const status = await getAccountsStatus();
  const account = status?.accounts[0];
  const pendingResult = account
    ? await apiFetch<AgentAction[]>(`/api/v1/agent/${account.id}/actions?pending_only=true`)
    : null;
  const pending = pendingResult?.ok ? pendingResult.data.length : 0;

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-surface">
        <div className="border-b border-border px-5 py-4">
          <p className="text-sm font-semibold tracking-tight">X Intelligence</p>
          <p className="mt-0.5 truncate text-xs text-text-muted">
            {account ? `@${account.username}` : "No account connected"}
          </p>
        </div>

        <nav className="flex-1 space-y-0.5 p-3">
          {SECTIONS.map((section) => (
            <NavLink
              key={section.href}
              href={section.href}
              label={section.label}
              badge={section.href === "/agent" ? pending : undefined}
            />
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
