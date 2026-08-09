"use server";

import { revalidatePath } from "next/cache";
import { cookies } from "next/headers";

import { SESSION_COOKIE } from "@/lib/api";

const BACKEND = process.env.BACKEND_INTERNAL_URL ?? "http://backend:8000";

export type ImportState = {
  error: string | null;
  summary: string | null;
  imported: number;
  duplicates: number;
  detectedColumns: Record<string, string>;
  rowErrors: { row: number; reason: string }[];
};

export const idleImport: ImportState = {
  error: null,
  summary: null,
  imported: 0,
  duplicates: 0,
  detectedColumns: {},
  rowErrors: [],
};

/**
 * Upload a revenue CSV.
 *
 * The file is streamed straight through to the backend rather than parsed here:
 * the deduplication fingerprint and the amount parsing both live server-side, so
 * there is exactly one implementation of what a row means.
 */
export async function importRevenueCsv(
  accountId: string,
  _prev: ImportState,
  formData: FormData,
): Promise<ImportState> {
  const file = formData.get("file");
  if (!(file instanceof File) || file.size === 0) {
    return { ...idleImport, error: "Choose a CSV file to import." };
  }

  const source = String(formData.get("default_source") ?? "OTHER");
  const currency = String(formData.get("default_currency") ?? "USD");

  const store = await cookies();
  const session = store.get(SESSION_COOKIE)?.value;

  const upload = new FormData();
  upload.append("file", file, file.name);

  let response: Response;
  try {
    response = await fetch(
      `${BACKEND}/api/v1/analytics/${accountId}/revenue/import` +
        `?default_source=${encodeURIComponent(source)}&default_currency=${encodeURIComponent(currency)}`,
      {
        method: "POST",
        headers: session ? { Cookie: `${SESSION_COOKIE}=${session}` } : {},
        body: upload,
        cache: "no-store",
      },
    );
  } catch {
    return { ...idleImport, error: "Could not reach the backend." };
  }

  const body = (await response.json().catch(() => null)) as Record<string, unknown> | null;

  if (!response.ok) {
    const error = body?.error as { message?: string } | undefined;
    return { ...idleImport, error: error?.message ?? "The import failed." };
  }

  revalidatePath("/revenue");
  return {
    error: null,
    summary: String(body?.summary ?? "Import complete."),
    imported: Number(body?.imported ?? 0),
    duplicates: Number(body?.duplicates ?? 0),
    detectedColumns: (body?.detected_columns as Record<string, string>) ?? {},
    rowErrors: (body?.errors as { row: number; reason: string }[]) ?? [],
  };
}
