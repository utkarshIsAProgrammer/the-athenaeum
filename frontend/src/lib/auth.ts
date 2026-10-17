/**
 * Account calls made from the browser.
 *
 * These go through the `/api/*` proxy on this origin, so the session cookie is
 * attached and stored by the browser itself — nothing here touches it.
 */

export interface AuthResult {
  ok: boolean;
  error?: string;
}

async function readError(response: Response, fallback: string): Promise<string> {
  const body = (await response.json().catch(() => null)) as { error?: unknown } | null;
  if (body && typeof body.error === "string" && body.error.trim()) return body.error;
  return `${fallback} (${response.status}).`;
}

async function send(path: string, body?: unknown): Promise<AuthResult> {
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });

    if (!response.ok) {
      return { ok: false, error: await readError(response, "Request failed") };
    }
    return { ok: true };
  } catch {
    return { ok: false, error: "The news service is unreachable." };
  }
}

export function signUp(email: string, password: string): Promise<AuthResult> {
  return send("/api/auth/register", { email, password });
}

export function signIn(email: string, password: string): Promise<AuthResult> {
  return send("/api/auth/login", { email, password });
}

export function signOut(): Promise<AuthResult> {
  return send("/api/auth/logout");
}
