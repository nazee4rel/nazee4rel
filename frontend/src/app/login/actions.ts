"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { SESSION_COOKIE } from "@/lib/api";

const BACKEND = process.env.BACKEND_INTERNAL_URL ?? "http://backend:8000";

export type AuthState = { error: string | null };

/**
 * Copy the backend's session cookie onto this app's own response.
 *
 * The cookie is httpOnly at both hops, so it is never readable from JavaScript;
 * the browser only ever sees a same-origin cookie for the Next.js app.
 */
async function adoptSessionCookie(response: Response): Promise<void> {
  const setCookie = response.headers.get("set-cookie");
  if (!setCookie) return;

  const match = /xagent_session=([^;]+)/.exec(setCookie);
  if (!match) return;

  const maxAgeMatch = /Max-Age=(\d+)/i.exec(setCookie);
  const store = await cookies();
  store.set(SESSION_COOKIE, match[1], {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: maxAgeMatch ? Number(maxAgeMatch[1]) : 60 * 60 * 24,
  });
}

async function post(path: string, body: unknown): Promise<Response> {
  return fetch(`${BACKEND}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
}

function messageFrom(payload: unknown, fallback: string): string {
  const body = payload as { error?: { message?: string }; detail?: unknown };
  if (body?.error?.message) return body.error.message;
  // FastAPI's own 422 validation errors use `detail`.
  if (Array.isArray(body?.detail) && body.detail.length > 0) {
    const first = body.detail[0] as { msg?: string };
    if (first?.msg) return first.msg;
  }
  return fallback;
}

export async function login(_prev: AuthState, formData: FormData): Promise<AuthState> {
  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");

  if (!email || !password) {
    return { error: "Enter your email and password." };
  }

  let response: Response;
  try {
    response = await post("/api/v1/auth/login", { email, password });
  } catch {
    return { error: "Could not reach the backend. Is the stack running?" };
  }

  if (!response.ok) {
    return { error: messageFrom(await response.json().catch(() => null), "Sign-in failed.") };
  }

  await adoptSessionCookie(response);
  redirect("/overview");
}

export async function register(_prev: AuthState, formData: FormData): Promise<AuthState> {
  const payload = {
    email: String(formData.get("email") ?? "").trim(),
    password: String(formData.get("password") ?? ""),
    display_name: String(formData.get("display_name") ?? "").trim(),
    // Captured at registration because posting-time analysis is meaningless
    // without knowing the account owner's local time.
    timezone: String(formData.get("timezone") ?? "UTC"),
  };

  if (!payload.email || !payload.password || !payload.display_name) {
    return { error: "All fields are required." };
  }

  let response: Response;
  try {
    response = await post("/api/v1/auth/register", payload);
  } catch {
    return { error: "Could not reach the backend. Is the stack running?" };
  }

  if (!response.ok) {
    return {
      error: messageFrom(await response.json().catch(() => null), "Registration failed."),
    };
  }

  await adoptSessionCookie(response);
  redirect("/overview");
}

export async function logout(): Promise<void> {
  const store = await cookies();
  const session = store.get(SESSION_COOKIE)?.value;

  if (session) {
    // Revoke server-side too, so the session dies rather than just being
    // forgotten by this browser.
    await fetch(`${BACKEND}/api/v1/auth/logout`, {
      method: "POST",
      headers: { Cookie: `${SESSION_COOKIE}=${session}` },
      cache: "no-store",
    }).catch(() => null);
  }

  store.delete(SESSION_COOKIE);
  redirect("/login");
}
