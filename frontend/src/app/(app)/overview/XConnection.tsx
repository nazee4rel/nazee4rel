"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import { connectX, disconnectX, idleState, reprobeX, type ActionState } from "./actions";

function Button({
  label,
  pendingLabel,
  variant = "primary",
}: {
  label: string;
  pendingLabel: string;
  variant?: "primary" | "ghost" | "danger";
}) {
  const { pending } = useFormStatus();
  const styles = {
    primary: "bg-accent text-canvas hover:opacity-90",
    ghost: "border border-border text-text-muted hover:text-text hover:bg-surface-raised",
    danger: "border border-negative/40 text-negative hover:bg-negative/10",
  }[variant];

  return (
    <button
      type="submit"
      disabled={pending}
      className={`rounded-lg px-4 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${styles}`}
    >
      {pending ? pendingLabel : label}
    </button>
  );
}

function Feedback({ state }: { state: ActionState }) {
  if (!state.error && !state.message) return null;
  return (
    <p
      role="status"
      className={`mt-3 text-sm ${state.error ? "text-negative" : "text-positive"}`}
    >
      {state.error ?? state.message}
    </p>
  );
}

export function ConnectXButton({ writeEnabled }: { writeEnabled: boolean }) {
  const [state, formAction] = useActionState(async () => connectX(), idleState);

  return (
    <div>
      <form action={formAction}>
        <Button label="Connect X account" pendingLabel="Redirecting to X…" />
      </form>
      <p className="mt-3 max-w-2xl text-xs leading-relaxed text-text-muted">
        You will be sent to X to approve access. Your X password is never
        requested, seen or stored — this is OAuth 2.0 with PKCE.{" "}
        {writeEnabled
          ? "Posting permission is included, but the agent can only draft posts; nothing publishes without your approval of that specific draft."
          : "Read-only permissions are requested, so the agent cannot post, follow, or change anything on your account."}
      </p>
    </div>
  );
}

export function AccountControls({
  accountId,
  username,
}: {
  accountId: string;
  username: string;
}) {
  const [probeState, probeAction] = useActionState(
    async () => reprobeX(accountId),
    idleState,
  );
  const [disconnectState, disconnectAction] = useActionState(
    async () => disconnectX(accountId),
    idleState,
  );

  return (
    <div>
      <div className="flex flex-wrap gap-2">
        <form action={probeAction}>
          <Button label="Re-run probe" pendingLabel="Probing…" variant="ghost" />
        </form>
        <form
          action={disconnectAction}
          onSubmit={(event) => {
            // Disconnecting revokes credentials. Collected history survives, but
            // reconnecting requires the full OAuth round trip again.
            if (
              !window.confirm(
                `Disconnect @${username}? Credentials will be revoked. Your collected history is kept.`,
              )
            ) {
              event.preventDefault();
            }
          }}
        >
          <Button label="Disconnect" pendingLabel="Disconnecting…" variant="danger" />
        </form>
      </div>
      <Feedback state={probeState} />
      <Feedback state={disconnectState} />
    </div>
  );
}
