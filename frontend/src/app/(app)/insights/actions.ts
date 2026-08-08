"use server";

import { revalidatePath } from "next/cache";

import { apiFetch } from "@/lib/api";
import type { AgentRun } from "@/components/agent";

export type AgentActionState = { error: string | null; message: string | null };

export const idleAgentState: AgentActionState = { error: null, message: null };

/**
 * Run a full agent cycle now.
 *
 * The backend runs it inline rather than queueing it, so a refusal to run —
 * "not enough history yet" — comes back as an answer rather than as silence.
 */
export async function runAgentCycle(accountId: string): Promise<AgentActionState> {
  const result = await apiFetch<AgentRun>(`/api/v1/agent/${accountId}/run`, { method: "POST" });

  if (!result.ok) {
    return { error: result.error.message, message: null };
  }

  const run = result.data;
  revalidatePath("/insights");
  revalidatePath("/agent");

  if (run.status === "SKIPPED") {
    return { error: null, message: run.summary ?? "The agent found too little data to reason over." };
  }
  if (run.status === "PARTIAL") {
    return {
      error: null,
      message: `Analysis ran but reasoning did not: ${run.error ?? "the model was unreachable"}.`,
    };
  }
  if (run.status === "FAILED") {
    return { error: run.error ?? "The run failed.", message: null };
  }

  const rejected =
    run.grounding_rejections > 0
      ? ` ${run.grounding_rejections} item(s) were discarded for citing figures that did not match the data.`
      : "";
  return {
    error: null,
    message:
      `${run.insights_created} insight(s) and ${run.recommendations_created} ` +
      `recommendation(s) stored.${rejected}`,
  };
}

export async function decideRecommendation(
  recommendationId: string,
  decision: "accept" | "dismiss",
): Promise<AgentActionState> {
  const result = await apiFetch<{ id: string; status: string }>(
    `/api/v1/agent/recommendations/${recommendationId}/${decision}`,
    { method: "POST" },
  );

  if (!result.ok) {
    return { error: result.error.message, message: null };
  }
  revalidatePath("/insights");
  return {
    error: null,
    message:
      decision === "accept"
        ? "Marked as adopted. It will be graded against what actually happens."
        : "Dismissed. It will not be graded — advice nobody took cannot be scored.",
  };
}
