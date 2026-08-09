/**
 * Server-side API helper.
 *
 * This module must never be imported into a Client Component. It reads
 * BACKEND_INTERNAL_URL, which is not a NEXT_PUBLIC_ variable and therefore
 * exists only on the server — the boundary that keeps the backend address, and
 * everything behind it, out of the browser bundle.
 */

import { cookies } from "next/headers";

const BACKEND = process.env.BACKEND_INTERNAL_URL ?? "http://backend:8000";

export const SESSION_COOKIE = "xagent_session";

export type ApiError = {
  error: { code: string; message: string; request_id?: string };
};

export type User = {
  id: string;
  email: string;
  display_name: string;
  role: "OWNER" | "ADMIN" | "VIEWER";
  timezone: string;
  totp_enabled: boolean;
  last_login_at: string | null;
  created_at: string;
};

export type CapabilityStatus =
  | "UNKNOWN"
  | "AVAILABLE"
  | "UNAVAILABLE"
  | "FORBIDDEN"
  | "ERROR";

export type Capability = {
  capability: string;
  status: CapabilityStatus;
  last_checked_at: string | null;
  last_error: string | null;
};

export type XAccount = {
  id: string;
  x_user_id: string;
  username: string;
  display_name: string | null;
  profile_image_url: string | null;
  is_active: boolean;
  connected_at: string | null;
  collection_started_at: string | null;
  capabilities: Capability[];
};

export type AccountsStatus = {
  connected: boolean;
  accounts: XAccount[];
  write_actions_enabled: boolean;
  billing_mode: string;
  monthly_budget_usd: number;
};

/** Fetch from the backend, forwarding the caller's session cookie. */
export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<{ ok: true; data: T } | { ok: false; status: number; error: ApiError["error"] }> {
  const store = await cookies();
  const session = store.get(SESSION_COOKIE)?.value;

  let response: Response;
  try {
    response = await fetch(`${BACKEND}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(session ? { Cookie: `${SESSION_COOKIE}=${session}` } : {}),
        ...init.headers,
      },
      // Analytics data changes on the collector's schedule, not on render.
      cache: "no-store",
    });
  } catch {
    // The backend being unreachable is an expected state during startup, so it
    // is surfaced as a normal error rather than crashing the page.
    return {
      ok: false,
      status: 503,
      error: { code: "backend_unreachable", message: "The backend is not responding." },
    };
  }

  if (!response.ok) {
    let error = { code: "unknown_error", message: `Request failed (${response.status}).` };
    try {
      const body = (await response.json()) as ApiError;
      if (body?.error) error = body.error;
    } catch {
      /* non-JSON error body; keep the generic message */
    }
    return { ok: false, status: response.status, error };
  }

  return { ok: true, data: (await response.json()) as T };
}

export async function getCurrentUser(): Promise<User | null> {
  const result = await apiFetch<User>("/api/v1/auth/me");
  return result.ok ? result.data : null;
}

export async function getAccountsStatus(): Promise<AccountsStatus | null> {
  const result = await apiFetch<AccountsStatus>("/api/v1/accounts");
  return result.ok ? result.data : null;
}
