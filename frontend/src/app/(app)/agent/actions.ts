"use server";

import { revalidatePath } from "next/cache";

import { apiFetch } from "@/lib/api";
import type { AgentAction } from "@/components/agent";

export type ApprovalState = { error: string | null; message: string | null };

export const idleApproval: ApprovalState = { error: null, message: null };

/**
 * Approve a queued action.
 *
 * Approval satisfies one gate only. The backend still checks the tier, the
 * write flag, the granted scope and the probed capability, so an approved
 * action can legitimately come back BLOCKED — which is reported here rather
 * than reported as success.
 */
export async function approveAction(actionId: string): Promise<ApprovalState> {
  const result = await apiFetch<AgentAction>(`/api/v1/agent/actions/${actionId}/approve`, {
    method: "POST",
    body: JSON.stringify({}),
  });

  if (!result.ok) {
    return { error: result.error.message, message: null };
  }
  revalidatePath("/agent");

  const action = result.data;
  if (action.status === "BLOCKED") {
    return { error: action.blocked_reason ?? "The action was blocked.", message: null };
  }
  if (action.status === "EXPIRED") {
    return {
      error: action.blocked_reason ?? "The approval window had already lapsed.",
      message: null,
    };
  }
  if (action.status === "FAILED") {
    return { error: action.error ?? "The action failed.", message: null };
  }
  return { error: null, message: "Approved and executed." };
}

export async function rejectAction(actionId: string): Promise<ApprovalState> {
  const result = await apiFetch<AgentAction>(`/api/v1/agent/actions/${actionId}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason: "Rejected from the approval queue." }),
  });

  if (!result.ok) {
    return { error: result.error.message, message: null };
  }
  revalidatePath("/agent");
  return { error: null, message: "Rejected. Nothing was done." };
}
