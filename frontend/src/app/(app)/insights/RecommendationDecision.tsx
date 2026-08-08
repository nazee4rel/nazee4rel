"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import { decideRecommendation, idleAgentState, type AgentActionState } from "./actions";

function Button({ label, tone }: { label: string; tone: "accept" | "dismiss" }) {
  const { pending } = useFormStatus();
  const className =
    tone === "accept"
      ? "border-positive/40 text-positive hover:bg-positive/10"
      : "border-border text-text-muted hover:bg-surface-raised";
  return (
    <button
      type="submit"
      disabled={pending}
      className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition disabled:opacity-50 ${className}`}
    >
      {pending ? "…" : label}
    </button>
  );
}

/**
 * Adopt or dismiss a recommendation.
 *
 * This is not just bookkeeping: the verification stage refuses to grade advice
 * that was dismissed, because what happened afterwards cannot confirm or refute
 * something nobody acted on.
 */
export default function RecommendationDecision({
  recommendationId,
  status,
}: {
  recommendationId: string;
  status: string;
}) {
  const [acceptState, acceptAction] = useActionState<AgentActionState, FormData>(
    decideRecommendation.bind(null, recommendationId, "accept"),
    idleAgentState,
  );
  const [dismissState, dismissAction] = useActionState<AgentActionState, FormData>(
    decideRecommendation.bind(null, recommendationId, "dismiss"),
    idleAgentState,
  );
  const message = acceptState.message ?? dismissState.message;
  const error = acceptState.error ?? dismissState.error;

  if (status !== "PROPOSED") {
    return (
      <p className="text-xs text-text-muted">
        {status === "ACCEPTED"
          ? "Adopted — this will be graded on its verification date."
          : "Dismissed — this will not be graded."}
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <form action={acceptAction}>
          <Button label="I'm doing this" tone="accept" />
        </form>
        <form action={dismissAction}>
          <Button label="Not doing this" tone="dismiss" />
        </form>
      </div>
      {error && (
        <p role="alert" className="text-xs text-negative">
          {error}
        </p>
      )}
      {message && <p className="text-xs text-text-muted">{message}</p>}
    </div>
  );
}
