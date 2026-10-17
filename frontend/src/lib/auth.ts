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

/** The code itself comes back only here, once, at the moment it is minted. */
export interface RecoveryCodeResult extends AuthResult {
  code?: string;
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

/**
 * Set a new password from a written-down recovery code.
 *
 * Works signed out, obviously — that is the point. The code is spent by the
 * call, so the account page has to be visited for a fresh one afterwards.
 */
export function recoverAccount(
  email: string,
  recoveryCode: string,
  password: string,
): Promise<AuthResult> {
  return send("/api/auth/recover", { email, recovery_code: recoveryCode, password });
}

/** Mint a recovery code. Requires a session, and returns the code exactly once. */
export async function issueRecoveryCode(): Promise<RecoveryCodeResult> {
  try {
    const response = await fetch("/api/auth/recovery-code", { method: "POST" });

    if (!response.ok) {
      return { ok: false, error: await readError(response, "Could not generate a code") };
    }

    const body = (await response.json().catch(() => null)) as { recovery_code?: unknown } | null;
    if (!body || typeof body.recovery_code !== "string" || !body.recovery_code) {
      return { ok: false, error: "The server did not return a code." };
    }
    return { ok: true, code: body.recovery_code };
  } catch {
    return { ok: false, error: "The news service is unreachable." };
  }
}
