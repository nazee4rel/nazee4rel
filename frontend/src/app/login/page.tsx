import { redirect } from "next/navigation";

import { apiFetch, getCurrentUser } from "@/lib/api";

import AuthForm from "./AuthForm";

/**
 * Decides between sign-in and first-run setup.
 *
 * The backend closes registration once the owner exists (single-tenant, per
 * decision 5), so the page asks it which mode applies rather than guessing:
 * a 403 from the register endpoint means an owner is already set up.
 */
async function ownerExists(): Promise<boolean> {
  const probe = await apiFetch("/api/v1/auth/register", {
    method: "POST",
    body: JSON.stringify({}),
  });
  // 403 => registration closed => owner exists.
  // 422 => endpoint open, our empty body was simply invalid.
  return !probe.ok && probe.status === 403;
}

export default async function LoginPage() {
  if (await getCurrentUser()) redirect("/overview");

  const isSetup = !(await ownerExists());

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">X Account Intelligence</h1>
          <p className="mt-2 text-sm text-text-muted">
            {isSetup
              ? "Create the owner account for this instance."
              : "Sign in to your dashboard."}
          </p>
        </div>

        <div className="rounded-xl border border-border bg-surface p-6 shadow-lg">
          <AuthForm mode={isSetup ? "register" : "login"} />
        </div>

        <p className="mt-6 text-center text-xs leading-relaxed text-text-muted">
          This password is for this dashboard only. Your X account is connected
          separately via OAuth&nbsp;2.0 — your X password is never requested,
          seen, or stored.
        </p>
      </div>
    </main>
  );
}
