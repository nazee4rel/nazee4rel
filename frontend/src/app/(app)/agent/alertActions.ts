"use server";

import { revalidatePath } from "next/cache";

import { apiFetch } from "@/lib/api";
import type { AlertRecord, AlertRuleRecord } from "@/components/alerts";

export type AlertActionState = { error: string | null; message: string | null };

export const idleAlertState: AlertActionState = { error: null, message: null };

/**
 * Mark an alert as seen.
 *
 * Not the same as resolving it — the backend refuses to close a stateful alert
 * whose condition is still true, and this action does not pretend otherwise.
 */
export async function acknowledgeAlert(alertId: string): Promise<AlertActionState> {
  const result = await apiFetch<AlertRecord>(`/api/v1/alerts/${alertId}/acknowledge`, {
    method: "POST",
  });
  if (!result.ok) {
    return { error: result.error.message, message: null };
  }
  revalidatePath("/agent");
  revalidatePath("/overview");
  return {
    error: null,
    message: result.data.is_stateful
      ? "Marked as seen. It will close itself once the condition clears."
      : "Marked as seen.",
  };
}

export async function setRuleEnabled(
  accountId: string,
  ruleKey: string,
  enabled: boolean,
): Promise<AlertActionState> {
  const result = await apiFetch<AlertRuleRecord>(
    `/api/v1/alerts/${accountId}/rules/${ruleKey}`,
    { method: "PATCH", body: JSON.stringify({ enabled }) },
  );
  if (!result.ok) {
    return { error: result.error.message, message: null };
  }
  revalidatePath("/agent");
  return {
    error: null,
    message: enabled ? "Rule enabled." : "Rule disabled. It will not fire again.",
  };
}

/** Run every enabled rule now, with the same dedupe and cooldown as the schedule. */
export async function evaluateAlertsNow(accountId: string): Promise<AlertActionState> {
  const result = await apiFetch<{
    findings: number;
    raised: number;
    suppressed_duplicate: number;
    suppressed_cooldown: number;
    resolved: number;
    summary: string;
  }>(`/api/v1/alerts/${accountId}/evaluate`, { method: "POST" });

  if (!result.ok) {
    return { error: result.error.message, message: null };
  }
  revalidatePath("/agent");
  revalidatePath("/overview");
  return { error: null, message: result.data.summary };
}

export async function generateReport(
  accountId: string,
  period: "DAILY" | "WEEKLY" | "MONTHLY",
): Promise<AlertActionState> {
  const result = await apiFetch<{ title: string }>(
    `/api/v1/alerts/${accountId}/reports?period=${period}&deliver=false`,
    { method: "POST" },
  );
  if (!result.ok) {
    return { error: result.error.message, message: null };
  }
  revalidatePath("/insights");
  return { error: null, message: `Generated: ${result.data.title}` };
}
