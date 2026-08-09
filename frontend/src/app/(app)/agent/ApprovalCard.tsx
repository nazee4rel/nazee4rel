"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import type { AgentAction } from "@/components/agent";

import { approveAction, idleApproval, rejectAction, type ApprovalState } from "./actions";

function Button({ label, tone }: { label: string; tone: "approve" | "reject" }) {
  const { pending } = useFormStatus();
  const className =
    tone === "approve"
      ? "bg-accent text-canvas hover:opacity-90"
      : "border border-border text-text-muted hover:bg-surface-raised";
  return (
    <button
      type="submit"
      disabled={pending}
      className={`rounded-lg px-3 py-1.5 text-xs font-medium transition disabled:opacity-50 ${className}`}
    >
      {pending ? "…" : label}
    </button>
  );
}

export default function ApprovalCard({ action }: { action: AgentAction }) {
  const [approveState, approve] = useActionState<ApprovalState, FormData>(
    approveAction.bind(null, action.id),
    idleApproval,
  );
  const [rejectState, reject] = useActionState<ApprovalState, FormData>(
    rejectAction.bind(null, action.id),
    idleApproval,
  );
  const error = approveState.error ?? rejectState.error;
  const message = approveState.message ?? rejectState.message;

  return (
    <article className="rounded-xl border border-accent/30 bg-accent/5 p-5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded border border-border bg-surface px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide">
          {action.autonomy_tier}
        </span>
        <span className="text-xs uppercase tracking-wide text-text-muted">
          {action.action_type.replace(/_/g, " ").toLowerCase()}
        </span>
        {action.expires_at && (
          <span className="ml-auto text-xs text-text-muted">
            expires {new Date(action.expires_at).toLocaleString()}
          </span>
        )}
      </div>

      <h3 className="mt-2 text-sm font-medium">{action.summary}</h3>

      {/* Written by the backend from the action's declared policy — never text
          the model produced, so what you are approving cannot be reworded by
          content the agent read. */}
      <p className="mt-2 text-sm leading-relaxed text-text-muted">{action.effect}</p>

      {Object.keys(action.payload).length > 0 && (
        <dl className="mt-3 space-y-1.5 rounded-lg border border-border bg-surface p-3 text-xs">
          {Object.entries(action.payload).map(([key, value]) => (
            <div key={key}>
              <dt className="text-text-muted">{key}</dt>
              <dd className="mt-0.5 break-words whitespace-pre-wrap">{value}</dd>
            </div>
          ))}
        </dl>
      )}

      {action.ingested_untrusted_content && (
        <p className="mt-3 rounded-lg border border-warning/40 bg-warning/5 px-3 py-2 text-xs leading-relaxed text-warning">
          The run that produced this read post text, which is free text this system
          did not author. Read the payload above as content the agent may have been
          influenced by, not as a neutral suggestion.
        </p>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <form action={approve}>
          <Button label="Approve" tone="approve" />
        </form>
        <form action={reject}>
          <Button label="Reject" tone="reject" />
        </form>
      </div>

      {error && (
        <p role="alert" className="mt-2 text-xs text-negative">
          {error}
        </p>
      )}
      {message && <p className="mt-2 text-xs text-positive">{message}</p>}
    </article>
  );
}
