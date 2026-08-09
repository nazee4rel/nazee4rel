"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import { login, register, type AuthState } from "./actions";

const initialState: AuthState = { error: null };

function SubmitButton({ label }: { label: string }) {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="w-full rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-canvas transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
    >
      {pending ? "Working…" : label}
    </button>
  );
}

const inputClass =
  "w-full rounded-lg border border-border bg-surface-raised px-3 py-2 text-sm text-text placeholder:text-text-muted focus:border-accent focus:outline-none";

export default function AuthForm({ mode }: { mode: "login" | "register" }) {
  const action = mode === "register" ? register : login;
  const [state, formAction] = useActionState(action, initialState);

  // Best-effort default so posting-time analysis starts out in the user's
  // actual local time rather than silently in UTC.
  const guessedTimezone =
    typeof Intl !== "undefined" ? Intl.DateTimeFormat().resolvedOptions().timeZone : "UTC";

  return (
    <form action={formAction} className="space-y-4">
      {mode === "register" && (
        <div>
          <label htmlFor="display_name" className="mb-1.5 block text-sm text-text-muted">
            Your name
          </label>
          <input id="display_name" name="display_name" required className={inputClass} />
        </div>
      )}

      <div>
        <label htmlFor="email" className="mb-1.5 block text-sm text-text-muted">
          Email
        </label>
        <input
          id="email"
          name="email"
          type="email"
          autoComplete="username"
          required
          className={inputClass}
        />
      </div>

      <div>
        <label htmlFor="password" className="mb-1.5 block text-sm text-text-muted">
          Password
        </label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete={mode === "register" ? "new-password" : "current-password"}
          required
          minLength={mode === "register" ? 12 : undefined}
          className={inputClass}
        />
        {mode === "register" && (
          <p className="mt-1.5 text-xs text-text-muted">
            At least 12 characters, mixing letters with numbers or symbols.
          </p>
        )}
      </div>

      {mode === "register" && <input type="hidden" name="timezone" value={guessedTimezone} />}

      {state.error && (
        <div
          role="alert"
          className="rounded-lg border border-negative/40 bg-negative/10 px-3 py-2 text-sm text-negative"
        >
          {state.error}
        </div>
      )}

      <SubmitButton label={mode === "register" ? "Create owner account" : "Sign in"} />
    </form>
  );
}
