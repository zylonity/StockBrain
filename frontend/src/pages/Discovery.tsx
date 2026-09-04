import { api } from "../api/client";
import { StatusPill } from "../components/StatusPill";
import { formatRelative, formatTimestamp } from "../components/formats";
import { usePolling } from "../components/usePolling";

export function Discovery() {
  const status = usePolling(api.discoveryStatus, 15_000);
  const topics = usePolling(api.discoveryTopics, 60_000);

  const stats = status.data?.stats;

  return (
    <>
      <div className="refresh-row">
        <div>
          <h1 className="page-title">Discovery</h1>
          <p className="page-subtitle" style={{ marginBottom: 0 }}>
            Ingestion throughput, scheduled work, and the thematic search topics
            that drive broad-web discovery.
          </p>
        </div>
        <button onClick={status.refresh} disabled={status.loading}>
          {status.loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {status.error && <div className="error">{status.error}</div>}

      <div className="grid">
        <div className="card">
          <h2>Subsystem</h2>
          <div className="metric">
            <StatusPill
              status={
                !status.data?.subsystem_running
                  ? "DOWN"
                  : status.data.paused || !status.data.discovery_enabled
                    ? "DISABLED"
                    : "HEALTHY"
              }
            />
          </div>
          <div className="metric-note">
            {!status.data
              ? "loading"
              : !status.data.subsystem_running
                ? "Workers and scheduler are not running"
                : status.data.paused
                  ? "Paused — new discovery suspended"
                  : status.data.discovery_enabled
                    ? "Running"
                    : "Disabled by configuration"}
          </div>
        </div>

        <div className="card">
          <h2>Sources ingested</h2>
          <div className="metric">{stats?.sources_total ?? "—"}</div>
          <div className="metric-note">
            {stats ? `${stats.sources_last_24h} in the last 24h` : "loading"}
          </div>
        </div>

        <div className="card">
          <h2>Events</h2>
          <div className="metric">{stats?.events_total ?? "—"}</div>
          <div className="metric-note">
            {stats
              ? `${stats.events_last_24h} in the last 24h · last source ${formatRelative(
                  stats.latest_source_at,
                )}`
              : "loading"}
          </div>
        </div>

        <div className="card">
          <h2>Jobs pending</h2>
          <div className="metric">{status.data?.jobs_pending ?? "—"}</div>
          <div className="metric-note">PostgreSQL-backed queue. No Redis.</div>
        </div>
      </div>

      <div className="grid">
        <div className="card">
          <h2>Sources by provider</h2>
          <table>
            <tbody>
              {Object.entries(stats?.sources_by_provider ?? {}).map(([name, count]) => (
                <tr key={name}>
                  <td className="mono">{name}</td>
                  <td className="mono">{count}</td>
                </tr>
              ))}
              {stats && Object.keys(stats.sources_by_provider).length === 0 && (
                <tr>
                  <td className="muted">Nothing ingested yet.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h2>Events by status</h2>
          <table>
            <tbody>
              {Object.entries(stats?.events_by_status ?? {}).map(([name, count]) => (
                <tr key={name}>
                  <td className="mono">{name}</td>
                  <td className="mono">{count}</td>
                </tr>
              ))}
              {stats && Object.keys(stats.events_by_status).length === 0 && (
                <tr>
                  <td className="muted">No events yet.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 18 }}>
        <h2>Scheduled tasks</h2>
        <table>
          <thead>
            <tr>
              <th>Task</th>
              <th>Every</th>
              <th>Last run</th>
              <th>Last error</th>
            </tr>
          </thead>
          <tbody>
            {(status.data?.scheduled_tasks ?? []).map((task) => (
              <tr key={task.name}>
                <td className="mono">{task.name}</td>
                <td className="mono">{Math.round(task.interval_seconds)}s</td>
                <td className="mono detail" title={formatTimestamp(task.last_run_at)}>
                  {formatRelative(task.last_run_at)}
                </td>
                <td className="detail">{task.last_error ?? "—"}</td>
              </tr>
            ))}
            {status.data && status.data.scheduled_tasks.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
                  No scheduled tasks running. Providers need credentials first.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>Search topics</h2>
        {topics.error && <div className="error">{topics.error}</div>}
        {(topics.data ?? []).map((topic) => (
          <div className="topic" key={topic.id}>
            <div className="topic-head">
              <span className="topic-name">{topic.name}</span>
              <StatusPill status={topic.enabled ? "HEALTHY" : "DISABLED"} />
              <span className="faint mono">
                every {topic.interval_minutes}m · {topic.freshness} · limit{" "}
                {topic.result_limit}
              </span>
              <span className="faint mono">
                last run {formatRelative(topic.last_run_at)}
              </span>
            </div>
            {topic.description && (
              <div className="metric-note">{topic.description}</div>
            )}
            <ul className="query-list">
              {topic.queries.map((query) => (
                <li key={query.id}>
                  <span className="query-text">{query.query}</span>
                  <span className="mono faint">
                    {query.results_seen} results · {query.credits_used} credits
                    {query.consecutive_failures > 0 &&
                      ` · ${query.consecutive_failures} failures`}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ))}
        {topics.data && topics.data.length === 0 && (
          <div className="placeholder">No topics configured.</div>
        )}
      </div>
    </>
  );
}
