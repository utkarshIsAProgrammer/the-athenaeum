/**
 * AI calls made from the browser: the reader's provider keys, and the summaries
 * those keys pay for.
 *
 * Everything here goes through the `/api/*` proxy on this origin, so the
 * session cookie is attached by the browser. A key travels one way only — it is
 * posted, never read back.
 */

import { errorMessage } from "./api";
import type { AiKeyState, SummaryResponse } from "./types";

export interface AiResult {
  ok: boolean;
  error?: string;
}

/**
 * Saving answers with the provider's new state, so the account page can show
 * exactly what the server stored rather than recomputing a hint from the key.
 */
export type KeySaveOutcome =
  | { ok: true; state: AiKeyState }
  | { ok: false; error: string };

export interface SummaryOutcome {
  ok: boolean;
  /** 0 when the request never reached the server. */
  status: number;
  error: string | null;
  data: SummaryResponse | null;
}

/** Where a free key for each provider is issued. */
export const KEY_PAGES: Record<string, string> = {
  groq: "https://console.groq.com/keys",
  openrouter: "https://openrouter.ai/keys",
  gemini: "https://aistudio.google.com/apikey",
};

function unreachable(): AiResult {
  return { ok: false, error: "The news service is unreachable." };
}

export async function saveAiKey(provider: string, key: string): Promise<KeySaveOutcome> {
  try {
    const response = await fetch(`/api/ai/keys/${provider}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ key }),
    });
    if (!response.ok) return { ok: false, error: await errorMessage(response) };
    return { ok: true, state: (await response.json()) as AiKeyState };
  } catch {
    return { ok: false, error: "The news service is unreachable." };
  }
}

export async function removeAiKey(provider: string): Promise<AiResult> {
  try {
    const response = await fetch(`/api/ai/keys/${provider}`, { method: "DELETE" });
    if (!response.ok) return { ok: false, error: await errorMessage(response) };
    return { ok: true };
  } catch {
    return unreachable();
  }
}

/**
 * Ask for a summary of one story. The server picks whichever stored key the
 * reader has, in its own order of preference, so no provider is named here.
 *
 * `signal` is what makes this cancellable: a summary takes tens of seconds, and
 * the reader should not have to sit through one.
 *
 * An abort rejects rather than returning an outcome, so callers can tell a
 * cancellation apart from a failure and stay quiet about it.
 */
export async function summarise(
  articleId: string,
  signal: AbortSignal,
): Promise<SummaryOutcome> {
  try {
    const response = await fetch("/api/ai/summarise", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ article_id: articleId }),
      signal,
    });

    if (!response.ok) {
      return { ok: false, status: response.status, error: await errorMessage(response), data: null };
    }
    return {
      ok: true,
      status: response.status,
      error: null,
      data: (await response.json()) as SummaryResponse,
    };
  } catch (cause) {
    if (signal.aborted) throw cause;
    return { ok: false, status: 0, error: "The news service is unreachable.", data: null };
  }
}
