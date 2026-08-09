"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import { generateReport } from "../agent/alertActions";
import { idleAlertState, type AlertActionState } from "../agent/alertActions";

function Submit({ label }: { label: string }) {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="rounded-lg border border-border px-2.5 py-1 text-xs text-text-muted transition hover:bg-surface-raised hover:text-text disabled:opacity-50"
    >
      {pending ? "…" : label}
    </button>
  );
}

/**
 * Generate a report on demand.
 *
 * Deliberately does not send it. Generating one to look at should not put an
 * email in someone's inbox, and re-running a period amends the stored report
 * rather than producing a second one.
 */
export default function ReportControls({ accountId }: { accountId: string }) {
  const [daily, dailyAction] = useActionState<AlertActionState, FormData>(
    generateReport.bind(null, accountId, "DAILY"),
    idleAlertState,
  );
  const [weekly, weeklyAction] = useActionState<AlertActionState, FormData>(
    generateReport.bind(null, accountId, "WEEKLY"),
    idleAlertState,
  );
  const [monthly, monthlyAction] = useActionState<AlertActionState, FormData>(
    generateReport.bind(null, accountId, "MONTHLY"),
    idleAlertState,
  );
  const message = daily.message ?? weekly.message ?? monthly.message;
  const error = daily.error ?? weekly.error ?? monthly.error;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <form action={dailyAction}>
          <Submit label="Daily" />
        </form>
        <form action={weeklyAction}>
          <Submit label="Weekly" />
        </form>
        <form action={monthlyAction}>
          <Submit label="Monthly" />
        </form>
      </div>
      {error && <p className="text-xs text-negative">{error}</p>}
      {message && <p className="text-xs text-text-muted">{message}</p>}
    </div>
  );
}
