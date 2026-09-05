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

        <div className="card">
          <h2>Classifier</h2>
          <div className="metric">
            <StatusPill
              status={
                !status.data
                  ? "UNKNOWN"
                  : !status.data.classifier_active
                    ? "DISABLED"
                    : status.data.budget?.status === "HARD_EXCEEDED"
                      ? "DOWN"
                      : status.data.budget?.status === "SOFT_EXCEEDED"
                        ? "DEGRADED"
                        : "HEALTHY"
              }
            />
          </div>
          <div className="metric-note">
            {status.data?.classifier_active
              ? status.data.classifier_model
              : "Not configured — events wait in NEW"}
          </div>
        </div>
      </div>

      {status.data?.budget && (
        <div className="card" style={{ marginBottom: 18 }}>
          <h2>LLM budget</h2>
          {status.data.budget.reason && (
            <div
              className={
                status.data.budget.status === "HARD_EXCEEDED" ? "error" : "banner banner-warn"
              }
            >
              {status.data.budget.reason}
            </div>
          )}
          <table>
            <thead>
              <tr>
                <th>Window</th>
                <th>Spent</th>
                <th>Soft limit</th>
                <th>Hard limit</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Today</td>
                <td className="mono">
                  ${Number(status.data.budget.daily_spend_usd).toFixed(4)}
                </td>
                <td className="mono detail">
                  ${Number(status.data.budget.daily_soft_usd).toFixed(2)}
                </td>
                <td className="mono detail">
                  ${Number(status.data.budget.daily_hard_usd).toFixed(2)}
                </td>
              </tr>
              <tr>
                <td>This month</td>
                <td className="mono">
                  ${Number(status.data.budget.monthly_spend_usd).toFixed(4)}
                </td>
                <td className="mono detail">
                  ${Number(status.data.budget.monthly_soft_usd).toFixed(2)}
                </td>
                <td className="mono detail">
                  ${Number(status.data.budget.monthly_hard_usd).toFixed(2)}
                </td>
              </tr>
            </tbody>
          </table>
          <p className="metric-note">
            At the soft limit, optional work such as semantic deduplication is
            suppressed. At the hard limit, no new model analysis starts —
            ingestion, deterministic deduplication and broker reconciliation
            continue regardless.
          </p>
        </div>
      )}

      {status.data?.queue && (
        <div className="card">
          <h2>Job queue</h2>
          <p className="metric-note">
            The queue <em>is</em> the audit trail, which only helps if somebody
            can read it. The number that matters is the age of the oldest
            pending job: a stopped pipeline looks identical to a healthy one
            from every other angle.
          </p>
          <table>
            <tbody>
              <tr>
                <td>Pending / running</td>
                <td className="mono">
                  {status.data.queue.pending} / {status.data.queue.running}
                </td>
              </tr>
              <tr>
                <td>Oldest pending</td>
                <td className="mono">
                  {status.data.queue.oldest_pending_age_seconds === null
                    ? "—"
                    : `${Math.round(status.data.queue.oldest_pending_age_seconds)}s`}
                  {status.data.queue.oldest_pending_job_type && (
                    <span className="detail"> {status.data.queue.oldest_pending_job_type}</span>
                  )}
                </td>
              </tr>
              <tr>
                <td>Stuck past the claim timeout</td>
                <td className="mono">
                  {status.data.queue.stuck}
                  {status.data.queue.stuck_job_types.length > 0 && (
                    <span className="detail">
                      {" "}
                      {status.data.queue.stuck_job_types.join(", ")}
                    </span>
                  )}
                </td>
              </tr>
              <tr>
                <td>Dead (no attempts left)</td>
                <td className="mono">{status.data.queue.dead}</td>
              </tr>
            </tbody>
          </table>
          {Object.keys(status.data.queue.counts_by_type).length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Job type</th>
                  <th>Count</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(status.data.queue.counts_by_type)
                  .sort(([, a], [, b]) => b - a)
                  .map(([name, count]) => (
                    <tr key={name}>
                      <td className="mono">{name}</td>
                      <td className="mono">{count}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {status.data?.firecrawl && (
        <div className="card">
          <h2>Firecrawl budget</h2>
          {status.data.firecrawl.blockers.length > 0 && (
            <ul className="reason-list">
              {status.data.firecrawl.blockers.map((blocker) => (
                <li key={blocker} className="muted">
                  {blocker}
                </li>
              ))}
            </ul>
          )}
          {status.data.firecrawl.exhausted_reasons.map((reason) => (
            <p key={reason} className="banner banner-warn">
              {reason}
            </p>
          ))}
          <p className="metric-note">
            Search costs 2 credits per 10 results and a content fetch costs 1
            per page, so the caps below are the real spending limit. Budget
            exhaustion degrades Firecrawl alone — Alpaca news, SEC EDGAR,
            classification, research and broker reconciliation all continue.
          </p>
          <table>
            <thead>
              <tr>
                <th>Window</th>
                <th>Used</th>
                <th>Cap</th>
                <th>Remaining</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Searches today</td>
                <td className="mono">{status.data.firecrawl.today.searches}</td>
                <td className="mono detail">{status.data.firecrawl.max_searches_per_day}</td>
                <td className="mono detail">
                  {status.data.firecrawl.searches_remaining_today}
                </td>
              </tr>
              <tr>
                <td>Content fetches today</td>
                <td className="mono">{status.data.firecrawl.today.scrapes}</td>
                <td className="mono detail">{status.data.firecrawl.max_scrapes_per_day}</td>
                <td className="mono detail">{status.data.firecrawl.scrapes_remaining_today}</td>
              </tr>
              <tr>
                <td>Credits today</td>
                <td className="mono">{status.data.firecrawl.today.estimated_credits}</td>
                <td className="mono detail">{status.data.firecrawl.daily_credit_cap}</td>
                <td className="mono detail">{status.data.firecrawl.daily_credits_remaining}</td>
              </tr>
              <tr>
                <td>Credits this month</td>
                <td className="mono">{status.data.firecrawl.month.estimated_credits}</td>
                <td className="mono detail">{status.data.firecrawl.monthly_credit_cap}</td>
                <td className="mono detail">{status.data.firecrawl.monthly_credits_remaining}</td>
              </tr>
            </tbody>
          </table>
          <p className="metric-note">
            Cadence floor {status.data.firecrawl.min_topic_interval_minutes} min ·
            limit {status.data.firecrawl.search_result_limit} per source ×{" "}
            {status.data.firecrawl.search_sources.join(", ")} · content fetches{" "}
            {status.data.firecrawl.scrape_enabled ? "on" : "off"} · last success{" "}
            {formatRelative(status.data.firecrawl.last_successful_call_at)} ·{" "}
            {status.data.firecrawl.today.provider_reported_credits} of today&apos;s credits
            confirmed by the provider
          </p>
          {status.data.firecrawl.per_topic.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Topic</th>
                  <th>Query</th>
                  <th>Last success</th>
                  <th>Next eligible</th>
                  <th>Interval</th>
                  <th>Fails</th>
                  <th>Credits</th>
                </tr>
              </thead>
              <tbody>
                {status.data.firecrawl.per_topic.map((row) => (
                  <tr key={`${row.topic}:${row.query}`} className={row.enabled ? "" : "muted"}>
                    <td className="mono">{row.topic}</td>
                    <td>{row.query}</td>
                    <td className="mono">
                      {row.last_success_at
                        ? new Date(row.last_success_at).toLocaleString()
                        : "—"}
                    </td>
                    <td className="mono">
                      {row.enabled
                        ? row.next_eligible_at
                          ? new Date(row.next_eligible_at).toLocaleString()
                          : "now"
                        : "disabled"}
                    </td>
                    <td className="mono">{row.effective_interval_minutes}m</td>
                    <td className="mono">{row.consecutive_failures}</td>
                    <td className="mono">{row.credits_used}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

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
