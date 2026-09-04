import { api } from "../api/client";
import { ExecutionBanner } from "../components/ExecutionBanner";
import { StatusPill } from "../components/StatusPill";
import { usePolling } from "../components/usePolling";

const REFRESH_MS = 15_000;

export function Dashboard() {
  const health = usePolling(api.health, REFRESH_MS);
  const readiness = usePolling(api.readiness, REFRESH_MS);
  const execution = usePolling(api.executionStatus, 60_000);

  return (
    <>
      <h1 className="page-title">Dashboard</h1>
      <p className="page-subtitle">
        Discovery, research and proposal pipelines land here as later phases
        ship. What is shown below is read from the API, never computed in the
        browser.
      </p>

      {execution.error && (
        <div className="error">Execution status unavailable: {execution.error}</div>
      )}
      {execution.data && <ExecutionBanner status={execution.data} />}

      {health.error && <div className="error">Health unavailable: {health.error}</div>}

      <div className="grid">
        <div className="card">
          <h2>Application</h2>
          <div className="metric">
            {health.data ? <StatusPill status={health.data.status} /> : "—"}
          </div>
          <div className="metric-note">
            {health.data
              ? `${health.data.app} ${health.data.version} · ${health.data.environment}`
              : "loading"}
          </div>
        </div>

        <div className="card">
          <h2>Database</h2>
          <div className="metric">
            {readiness.data ? <StatusPill status={readiness.data.database} /> : "—"}
          </div>
          <div className="metric-note">
            {readiness.data
              ? readiness.data.schema_current
                ? "Schema at expected migration revision"
                : (readiness.data.detail ?? "Schema out of date")
              : "loading"}
          </div>
        </div>

        <div className="card">
          <h2>Ready to serve</h2>
          <div className="metric">
            {readiness.data ? (readiness.data.ready ? "YES" : "NO") : "—"}
          </div>
          <div className="metric-note">
            Requires PostgreSQL reachable and migrations applied.
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Subsystems</h2>
        <table>
          <thead>
            <tr>
              <th>Subsystem</th>
              <th>Status</th>
              <th>Providers</th>
            </tr>
          </thead>
          <tbody>
            {(health.data?.subsystems ?? []).map((subsystem) => (
              <tr key={subsystem.subsystem}>
                <td className="mono">{subsystem.subsystem}</td>
                <td>
                  <StatusPill status={subsystem.status} />
                </td>
                <td className="mono detail">{subsystem.providers.join(", ")}</td>
              </tr>
            ))}
            {!health.data && (
              <tr>
                <td colSpan={3} className="muted">
                  Loading…
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="grid" style={{ marginTop: 18 }}>
        <div className="card">
          <h2>Latest events</h2>
          <div className="placeholder">Ingestion lands in phase 2.</div>
        </div>
        <div className="card">
          <h2>Pending proposals</h2>
          <div className="placeholder">Proposals land in phase 6.</div>
        </div>
        <div className="card">
          <h2>Portfolio</h2>
          <div className="placeholder">Broker reconciliation lands in phase 8.</div>
        </div>
      </div>
    </>
  );
}
