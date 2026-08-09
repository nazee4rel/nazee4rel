"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { apiFetch } from "@/lib/api";

export type ActionState = { error: string | null; message: string | null };

export const idleState: ActionState = { error: null, message: null };

/**
 * Begin the X OAuth handshake.
 *
 * The backend mints the state and PKCE verifier and stores them server-side;
 * we only ever receive the URL to send the user to. The verifier never reaches
 * the browser.
 */
export async function connectX(): Promise<ActionState> {
  const result = await apiFetch<{ authorize_url: string }>("/api/v1/x/oauth/start", {
    method: "POST",
  });

  if (!result.ok) {
    return { error: result.error.message, message: null };
  }
  // Off-origin redirect to x.com; Next.js signals this by throwing, so it must
  // not sit inside a try/catch.
  redirect(result.data.authorize_url);
}

export async function disconnectX(accountId: string): Promise<ActionState> {
  const result = await apiFetch<{ message: string }>(`/api/v1/x/accounts/${accountId}`, {
    method: "DELETE",
  });

  if (!result.ok) {
    return { error: result.error.message, message: null };
  }
  revalidatePath("/overview");
  return { error: null, message: result.data.message };
}

export async function reprobeX(accountId: string): Promise<ActionState> {
  const result = await apiFetch<{ capability: string; status: string }[]>(
    `/api/v1/x/accounts/${accountId}/probe`,
    { method: "POST" },
  );

  if (!result.ok) {
    return { error: result.error.message, message: null };
  }

  const available = result.data.filter((r) => r.status === "AVAILABLE").length;
  revalidatePath("/overview");
  return {
    error: null,
    message: `Probe complete: ${available} of ${result.data.length} capabilities available.`,
  };
}
