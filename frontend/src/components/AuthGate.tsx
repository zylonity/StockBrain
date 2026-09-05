import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../api/client";
import type { SessionView } from "../api/types";

/**
 * Renders the application only for an authenticated browser.
 *
 * Deliberately a thin gate rather than a router guard: the server refuses every
 * protected route on its own, so this exists to show a login form instead of a
 * page full of 401 errors. It is not the security boundary and must never be
 * mistaken for one -- deleting this file would make the UI unusable, not
 * insecure.
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<SessionView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      setSession(await api.session());
      setError(null);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (loading) {
    return <div className="auth-shell">Checking session…</div>;
  }

  if (session && (!session.auth_required || session.authenticated)) {
    return <>{children}</>;
  }

  return (
    <LoginForm
      blockers={session?.blockers ?? []}
      initialError={error}
      onSignedIn={refresh}
    />
  );
}

function LoginForm({
  blockers,
  initialError,
  onSignedIn,
}: {
  blockers: string[];
  initialError: string | null;
  onSignedIn: () => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(initialError);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api.login(username, password);
      if (result.authenticated) {
        onSignedIn();
        return;
      }
      setError("Incorrect username or password.");
    } catch (cause) {
      // A 401 is the expected wrong-password answer; anything else is worth
      // showing verbatim, because the useful case is a 503 naming the missing
      // WEB_OWNER_PASSWORD_HASH.
      setError(
        cause instanceof ApiError && cause.status === 401
          ? "Incorrect username or password."
          : cause instanceof ApiError
            ? cause.message
            : String(cause),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-shell">
      <form className="auth-card" onSubmit={submit}>
        <h1>StockBrain</h1>
        <p className="muted">
          This interface can authorize real broker orders. Sign in to continue.
        </p>
        {blockers.length > 0 && (
          <ul className="auth-blockers">
            {blockers.map((blocker) => (
              <li key={blocker}>{blocker}</li>
            ))}
          </ul>
        )}
        <label htmlFor="auth-username">Username</label>
        <input
          id="auth-username"
          autoComplete="username"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          required
        />
        <label htmlFor="auth-password">Password</label>
        <input
          id="auth-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
        />
        {error && <p className="auth-error">{error}</p>}
        <button type="submit" disabled={busy || blockers.length > 0}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
