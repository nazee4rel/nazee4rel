"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import { idleAgentState, runAgentCycle, type AgentActionState } from "./actions";

function Submit() {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-canvas transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
    >
      {pending ? "Running…" : "Run the agent now"}
    </button>
  );
}

export default function RunAgentButton({ accountId }: { accountId: string }) {
  const [state, formAction] = useActionState<AgentActionState, FormData>(
    runAgentCycle.bind(null, accountId),
    idleAgentState,
  );

  return (
    <div className="space-y-2">
      <form action={formAction}>
        <Submit />
      </form>
      {state.error && (
        <p role="alert" className="text-sm text-negative">
          {state.error}
        </p>
      )}
      {state.message && <p className="text-sm text-text-muted">{state.message}</p>}
    </div>
  );
}
