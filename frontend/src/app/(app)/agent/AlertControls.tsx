"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import type { AlertRuleRecord } from "@/components/alerts";

import {
  acknowledgeAlert,
  evaluateAlertsNow,
  idleAlertState,
  setRuleEnabled,
  type AlertActionState,
} from "./alertActions";

function SmallButton({ label, busyLabel }: { label: string; busyLabel?: string }) {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="rounded-lg border border-border px-2.5 py-1 text-xs text-text-muted transition hover:bg-surface-raised hover:text-text disabled:opacity-50"
    >
      {pending ? (busyLabel ?? "…") : label}
    </button>
  );
}

export function AcknowledgeButton({ alertId }: { alertId: string }) {
  const [state, action] = useActionState<AlertActionState, FormData>(
    acknowledgeAlert.bind(null, alertId),
    idleAlertState,
  );

  if (state.message) {
    return <p className="text-xs text-text-muted">{state.message}</p>;
  }

  return (
    <form action={action} className="flex items-center gap-2">
      <SmallButton label="Mark as seen" />
      {state.error && <span className="text-xs text-negative">{state.error}</span>}
    </form>
  );
}

export function EvaluateNowButton({ accountId }: { accountId: string }) {
  const [state, action] = useActionState<AlertActionState, FormData>(
    evaluateAlertsNow.bind(null, accountId),
    idleAlertState,
  );

  return (
    <div className="space-y-2">
      <form action={action}>
        <SmallButton label="Check now" busyLabel="Checking…" />
      </form>
      {state.error && <p className="text-xs text-negative">{state.error}</p>}
      {state.message && <p className="text-xs text-text-muted">{state.message}</p>}
    </div>
  );
}

/**
 * A rule row, with its cooldown shown rather than buried.
 *
 * The cooldown is the setting that decides whether this system stays worth
 * listening to, so it belongs on the row next to the switch — not behind an
 * "advanced" disclosure.
 */
export function RuleToggle({
  accountId,
  rule,
}: {
  accountId: string;
  rule: AlertRuleRecord;
}) {
  const [state, action] = useActionState<AlertActionState, FormData>(
    setRuleEnabled.bind(null, accountId, rule.rule_key, !rule.enabled),
    idleAlertState,
  );

  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border/50 py-3 last:border-0">
      <div className="max-w-2xl">
        <div className="flex flex-wrap items-center gap-2">
          <p className={`text-sm ${rule.enabled ? "" : "text-text-muted line-through"}`}>
            {rule.title}
          </p>
          {rule.is_stateful && (
            <span
              className="rounded border border-border px-1.5 py-0.5 text-[10px] uppercase text-text-muted"
              title="Describes a condition that ends. Resolves itself when it clears."
            >
              condition
            </span>
          )}
        </div>
        <p className="mt-1 text-xs leading-relaxed text-text-muted">{rule.description}</p>
        <p className="mt-1 text-xs text-text-muted">
          Quiet for {rule.cooldown_hours}h after firing · {rule.channels.join(", ").toLowerCase()}
        </p>
        {state.error && <p className="mt-1 text-xs text-negative">{state.error}</p>}
      </div>
      <form action={action}>
        <SmallButton label={rule.enabled ? "Disable" : "Enable"} />
      </form>
    </div>
  );
}
