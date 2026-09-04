import { api } from "../api/client";
import { StatusPill } from "../components/StatusPill";
import { usePolling } from "../components/usePolling";

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toISOString().replace("T", " ").slice(0, 19) + "Z";
}

export function SystemHealth() {
  const providers = usePolling(api.providers, 15_000);

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
