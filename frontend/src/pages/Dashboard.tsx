import { Link } from "react-router-dom";

import { api } from "../api/client";
import { EventStatusBadge, ProviderBadge } from "../components/Badges";
import { ExecutionBanner } from "../components/ExecutionBanner";
import { StatusPill } from "../components/StatusPill";
import { formatRelative } from "../components/formats";
import { usePolling } from "../components/usePolling";

const REFRESH_MS = 15_000;

/** Stable reference so usePolling does not refetch on every render. */
const recentEvents = () => api.events({ limit: 8 });

export function Dashboard() {
  const health = usePolling(api.health, REFRESH_MS);
  const readiness = usePolling(api.readiness, REFRESH_MS);
  const execution = usePolling(api.executionStatus, 60_000);
  const discovery = usePolling(api.discoveryStatus, REFRESH_MS);
  const latest = usePolling(recentEvents, REFRESH_MS);

  return (
    <>
      <h1 className="page-title">Dashboard</h1>
      <p className="page-subtitle">
        Everything below is read from the API and never recomputed in the
        browser. Research and proposal panels fill in as later phases ship.
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
          <h2>Sources (24h)</h2>
          <div className="metric">{discovery.data?.stats.sources_last_24h ?? "—"}</div>
          <div className="metric-note">
            {discovery.data
              ? `${discovery.data.stats.sources_total} total · last ${formatRelative(
                  discovery.data.stats.latest_source_at,
                )}`
              : "loading"}
          </div>
        </div>
        <div className="card">
          <h2>Events (24h)</h2>
          <div className="metric">{discovery.data?.stats.events_last_24h ?? "—"}</div>
          <div className="metric-note">
            {discovery.data
              ? `${discovery.data.stats.events_total} total · ${discovery.data.jobs_pending} jobs pending`
              : "loading"}
          </div>
        </div>
        <div className="card">
          <h2>Discovery</h2>
          <div className="metric">
            <StatusPill
              status={
                !discovery.data?.subsystem_running
                  ? "DOWN"
                  : discovery.data.paused || !discovery.data.discovery_enabled
                    ? "DISABLED"
                    : "HEALTHY"
              }
            />
          </div>
          <div className="metric-note">
            <Link to="/discovery">Topics and schedules</Link>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 18 }}>
        <h2>Latest events</h2>
        <table>
          <tbody>
            {(latest.data?.events ?? []).map((event) => (
              <tr key={event.id}>
                <td>
                  <Link className="event-title" to={`/events/${event.id}`}>
                    {event.title}
                  </Link>
                  <div className="event-meta">
                    {event.providers.map((name) => (
                      <ProviderBadge key={name} provider={name} />
                    ))}
                  </div>
                </td>
                <td style={{ width: 1 }}>
                  <EventStatusBadge status={event.status} />
                </td>
                <td className="mono detail" style={{ width: 1, whiteSpace: "nowrap" }}>
                  {formatRelative(event.first_seen_at)}
                </td>
              </tr>
            ))}
            {latest.data && latest.data.events.length === 0 && (
              <tr>
                <td className="muted">
                  Nothing ingested yet. Discovery providers need credentials
                  before news, filings or searches arrive.
                </td>
              </tr>
            )}
            {!latest.data && (
              <tr>
                <td className="muted">Loading…</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="grid">
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
