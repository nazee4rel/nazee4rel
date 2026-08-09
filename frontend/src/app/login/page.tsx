import { redirect } from "next/navigation";

import { apiFetch, getCurrentUser } from "@/lib/api";

import AuthForm from "./AuthForm";

/**
 * Decides between sign-in and first-run setup.
 *
 * The backend closes registration once the owner exists (single-tenant, per
 * decision 5) and answers this question directly. It used to be inferred from
 * a `POST /auth/register` probe, reading 403 as "owner exists" — which turned
 * every other status into "registration is open", including the 429 that
 * endpoint's own 5/hour limit returns. Five page loads showed a first-run
 * setup form on a fully configured instance, and the owner could not sign in.
 *
 * Note which way the fallback points: an unreadable answer means *sign in*.
 * Showing a sign-in form to someone who needs to register is a dead end they
 * can back out of; showing a setup form on a live instance is not.
 */
async function ownerExists(): Promise<boolean> {
  const result = await apiFetch<{ owner_exists: boolean }>("/api/v1/auth/setup-status");
  return result.ok ? result.data.owner_exists : true;
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
