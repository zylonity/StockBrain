import { useState } from "react";

import { ApiError, api } from "../api/client";
import { ControlBanner } from "../components/ControlBanner";
import { StatusPill } from "../components/StatusPill";
import { usePolling } from "../components/usePolling";

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toISOString().replace("T", " ").slice(0, 19) + "Z";
}

export function SystemHealth() {
  const providers = usePolling(api.providers, 15_000);
  const control = usePolling(api.controlState, 15_000);
  const telegram = usePolling(api.telegramStatus, 30_000);
  const [busy, setBusy] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);

  /**
   * Every control change goes to the server and the answer is re-read from it.
   * Nothing here decides anything locally: the durable row is the state, and
   * the button is only a request to change it.
   */
  async function change(action: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    setControlError(null);
    try {
      await action();
    } catch (cause) {
      setControlError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setBusy(false);
      await control.refresh();
    }
  }

  const halted = control.data?.trading_halted ?? false;
  const killed = control.data?.kill_switch.active ?? false;
  const paused = control.data?.paused.active ?? false;

  return (
    <>
      <div className="refresh-row">
        <div>
          <h1 className="page-title">System health</h1>
          <p className="page-subtitle" style={{ marginBottom: 0 }}>
            A DOWN provider degrades only its own subsystem. DISABLED means not
            configured or deliberately switched off, and is never a fault.
          </p>
        </div>
        <button onClick={providers.refresh} disabled={providers.loading}>
          {providers.loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {providers.error && <div className="error">{providers.error}</div>}

      {control.data && <ControlBanner control={control.data} />}

      <div className="card">
        <h2>Execution control</h2>
        <p className="detail">
          Pausing stops new proposals and every authorization path — web,
          Telegram and automatic. The kill switch does the same and additionally
          denies any future order transmission. Neither closes a position and
          neither cancels a broker order: StockBrain has no order, cancel or
          amend path. Both are stored in PostgreSQL, so they survive a restart.
        </p>
        <div className="refresh-row">
          <span className="mono">
            {halted ? (killed ? "KILL SWITCH ENGAGED" : "PAUSED") : "running"}
          </span>
          <span>
            <button
              onClick={() => change(() => api.pauseTrading("paused from the web GUI"))}
              disabled={busy || paused}
            >
              Pause
            </button>{" "}
            <button
              onClick={() => change(() => api.resumeTrading("resumed from the web GUI"))}
              disabled={busy || !paused}
            >
              Resume
            </button>{" "}
            <button
              onClick={() =>
                change(() => api.setKillSwitch(true, "emergency stop from the web GUI"))
              }
              disabled={busy || killed}
            >
              Engage kill switch
            </button>{" "}
            <button
              onClick={() =>
                change(() => api.setKillSwitch(false, "released from the web GUI"))
              }
              disabled={busy || !killed}
            >
              Release kill switch
            </button>
          </span>
        </div>
        {controlError && <div className="error">{controlError}</div>}
      </div>

      <div className="card">
        <h2>Telegram</h2>
        {telegram.data ? (
          <table>
            <tbody>
              <tr>
                <td>Status</td>
                <td>
                  <StatusPill status={telegram.data.status} />
                </td>
              </tr>
              <tr>
                <td>Transport</td>
                <td className="mono">
                  {telegram.data.transport} · webhook{" "}
                  {telegram.data.webhook_configured ? "configured" : "none"}
                </td>
              </tr>
              <tr>
                <td>Bot</td>
                <td className="mono">
                  {telegram.data.bot_configured ? "configured" : "not configured"} ·{" "}
                  {telegram.data.bot_identified ? "authenticated" : "not authenticated"}
                </td>
              </tr>
              <tr>
                <td>Last contact</td>
                <td className="mono detail">
                  {formatTimestamp(telegram.data.last_contact_at)}
                </td>
              </tr>
              <tr>
                <td>Last error</td>
                <td className="mono detail">
                  {telegram.data.last_error_category ?? "—"} (
                  {telegram.data.consecutive_failures} consecutive)
                </td>
              </tr>
              <tr>
                <td>Authorized</td>
                <td className="mono">
                  {telegram.data.authorized_users} user(s) ·{" "}
                  {telegram.data.authorized_chats} chat(s) ·{" "}
                  {telegram.data.notification_targets} notification target(s)
                </td>
              </tr>
              {telegram.data.blockers.length > 0 && (
                <tr>
                  <td>Blockers</td>
                  <td className="detail">{telegram.data.blockers.join("; ")}</td>
                </tr>
              )}
            </tbody>
          </table>
        ) : (
          <p className="muted">Loading…</p>
        )}
      </div>

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Provider</th>
              <th>Status</th>
              <th>Detail</th>
              <th>Last OK</th>
              <th>Last checked</th>
              <th>Failures</th>
            </tr>
          </thead>
          <tbody>
            {(providers.data?.providers ?? []).map((provider) => (
              <tr key={provider.provider}>
                <td className="mono">{provider.provider}</td>
                <td>
                  <StatusPill status={provider.status} />
                </td>
                <td className="detail">{provider.detail ?? "—"}</td>
                <td className="mono detail">{formatTimestamp(provider.last_ok_at)}</td>
                <td className="mono detail">
                  {formatTimestamp(provider.last_checked_at)}
                </td>
                <td className="mono">{provider.consecutive_failures}</td>
              </tr>
            ))}
            {!providers.data && (
              <tr>
                <td colSpan={6} className="muted">
                  Loading…
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
