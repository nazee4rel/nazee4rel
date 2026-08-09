"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import { idleImport, importRevenueCsv, type ImportState } from "./actions";

const SOURCES = [
  "X_ADS_SHARE",
  "X_SUBSCRIPTIONS",
  "X_TIPS",
  "SPONSORSHIP",
  "AFFILIATE",
  "BRAND_DEAL",
  "OTHER",
];

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-canvas transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
    >
      {pending ? "Importing…" : "Import CSV"}
    </button>
  );
}

const fieldClass =
  "rounded-lg border border-border bg-surface-raised px-3 py-2 text-sm text-text focus:border-accent focus:outline-none";

export default function RevenueImport({ accountId }: { accountId: string }) {
  const [state, formAction] = useActionState<ImportState, FormData>(
    importRevenueCsv.bind(null, accountId),
    idleImport,
  );

  return (
    <section className="rounded-xl border border-border bg-surface p-5">
      <h2 className="text-sm font-medium">Import revenue CSV</h2>
      <p className="mt-2 max-w-2xl text-xs leading-relaxed text-text-muted">
        Column names are detected automatically — date, amount, currency, source
        and description are recognised under their common aliases. Rows are
        fingerprinted, so importing the same statement twice adds nothing rather
        than doubling your totals. Anything unreadable is reported per row instead
        of being silently treated as zero.
      </p>

      <form action={formAction} className="mt-4 flex flex-wrap items-end gap-3">
        <div>
          <label htmlFor="file" className="mb-1.5 block text-xs text-text-muted">
            CSV file
          </label>
          <input
            id="file"
            name="file"
            type="file"
            accept=".csv,text/csv"
            required
            className="block max-w-xs text-sm text-text-muted file:mr-3 file:rounded-md file:border-0 file:bg-surface-raised file:px-3 file:py-1.5 file:text-sm file:text-text"
          />
        </div>

        <div>
          <label htmlFor="default_source" className="mb-1.5 block text-xs text-text-muted">
            Source if not in file
          </label>
          <select id="default_source" name="default_source" className={fieldClass}>
            {SOURCES.map((source) => (
              <option key={source} value={source}>
                {source.replace(/_/g, " ").toLowerCase()}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="default_currency" className="mb-1.5 block text-xs text-text-muted">
            Currency
          </label>
          <input
            id="default_currency"
            name="default_currency"
            defaultValue="USD"
            maxLength={3}
            className={`${fieldClass} w-20 uppercase`}
          />
        </div>

        <SubmitButton />
      </form>

      {state.error && (
        <p role="alert" className="mt-3 text-sm text-negative">
          {state.error}
        </p>
      )}

      {state.summary && (
        <div className="mt-3 space-y-2">
          <p className="text-sm text-positive">{state.summary}</p>
          {Object.keys(state.detectedColumns).length > 0 && (
            <p className="text-xs text-text-muted">
              Detected columns:{" "}
              {Object.entries(state.detectedColumns)
                .map(([field, column]) => `${field} → ${column}`)
                .join(", ")}
            </p>
          )}
          {state.rowErrors.length > 0 && (
            <details className="text-xs text-warning">
              <summary className="cursor-pointer">
                {state.rowErrors.length} row(s) could not be read
              </summary>
              <ul className="mt-2 space-y-1">
                {state.rowErrors.slice(0, 20).map((rowError) => (
                  <li key={`${rowError.row}-${rowError.reason}`}>
                    Row {rowError.row}: {rowError.reason}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
    </section>
  );
}
