import { Link } from "react-router-dom";

import { api } from "../api/client";
import { EmptyState, PageHeader, RefreshButton, TableWrap } from "../components/Page";
import { StatusPill } from "../components/StatusPill";
import { formatRelative, formatTimestamp } from "../components/formats";
import { usePolling } from "../components/usePolling";

export function Discovery() {
  const status = usePolling(api.discoveryStatus, 15_000);
  const topics = usePolling(api.discoveryTopics, 60_000);

  const stats = status.data?.stats;

  return (
    <>
      <PageHeader
        title="Discovery"
        subtitle="Ingestion throughput, scheduled work, what each paid provider is allowed to spend, and the thematic search topics that drive broad-web discovery."
        actions={
          <>
            <Link className="button-quiet" to="/logs?categories=discovery">
              View logs
            </Link>
            <Link className="button-quiet" to="/settings">
              Settings
            </Link>
            <RefreshButton onClick={status.refresh} busy={status.loading} />
          </>
        }
      />

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
          <TableWrap>
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
          </TableWrap>
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
          <TableWrap>
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
          </TableWrap>
          {Object.keys(status.data.queue.counts_by_type).length > 0 && (
            <TableWrap>
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
            </TableWrap>
          )}
        </div>
      )}

      {status.data?.web_discovery && (
        <div className="card">
          <h2>Web discovery providers</h2>
          <p className="metric-note">
            Routine thematic search runs on{" "}
            <strong>{status.data.web_discovery.routine_provider}</strong>; semantic
            second-order search runs on{" "}
            <strong>{status.data.web_discovery.semantic_provider}</strong>. There is
            deliberately no automatic fallback between them — if one is
            unavailable its queries defer rather than moving onto a provider that
            costs more. Budget exhaustion degrades that provider alone; Alpaca
            news, SEC EDGAR, classification, research and broker reconciliation
            all continue.
          </p>
          <TableWrap>
          <table>
            <thead>
              <tr>
                <th>Provider</th>
                <th>Status</th>
                <th>Today</th>
                <th>Cap/day</th>
                <th>Month</th>
                <th>Cap/month</th>
                <th>Cost so far</th>
                <th>Last success</th>
              </tr>
            </thead>
            <tbody>
              {status.data.web_discovery.providers.map((provider) => (
                <tr key={provider.provider} className={provider.enabled ? "" : "muted"}>
                  <td className="mono">{provider.provider}</td>
                  <td>
                    <StatusPill status={provider.status} />
                  </td>
                  <td className="mono">
                    {provider.today.estimated_units} {provider.unit_label}
                  </td>
                  <td className="mono detail">{provider.daily_unit_cap}</td>
                  <td className="mono">{provider.month.estimated_units}</td>
                  <td className="mono detail">{provider.monthly_unit_cap}</td>
                  <td className="mono detail">
                    {/* Firecrawl bills credits against an allowance rather than
                        dollars per call, so it reports no price and the panel
                        says so rather than showing a fabricated $0.00. */}
                    {provider.unit_label === "credits"
                      ? "—"
                      : `$${Number(provider.month.estimated_cost_usd).toFixed(3)}`}
                  </td>
                  <td className="mono detail">
                    {formatRelative(provider.last_successful_call_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </TableWrap>
          {status.data.web_discovery.providers.flatMap((provider) =>
            provider.exhausted_reasons.map((reason) => (
              <p key={`${provider.provider}:${reason}`} className="banner banner-warn">
                {provider.provider}: {reason}
              </p>
            )),
          )}
          <ul className="reason-list">
            {status.data.web_discovery.providers.flatMap((provider) =>
              provider.blockers.map((blocker) => (
                <li key={`${provider.provider}:${blocker}`} className="muted">
                  {provider.provider}: {blocker}
                </li>
              )),
            )}
          </ul>
          <p className="metric-note">
            Cadence floors: routine{" "}
            {status.data.web_discovery.routine_min_interval_minutes} min, semantic{" "}
            {status.data.web_discovery.semantic_min_interval_minutes} min. No query
            can run faster than its floor, whatever its stored interval says.
          </p>
        </div>
      )}

      {status.data?.web_discovery?.extraction && (
        <div className="card">
          <h2>Content extraction</h2>
          <p className="metric-note">
            Pages are read only after the classifier shortlists them, locally
            first with {status.data.web_discovery.extraction.extractor}. The paid
            Firecrawl fallback runs only when a local attempt failed for a reason
            a different fetcher could fix, and is currently{" "}
            <strong>
              {status.data.web_discovery.extraction.fallback_enabled ? "on" : "off"}
            </strong>
            .
          </p>
          <TableWrap>
          <table>
            <tbody>
              <tr>
                <td>Pages read today</td>
                <td className="mono">
                  {status.data.web_discovery.extraction.fetched_today}
                </td>
                <td className="mono detail">
                  cap {status.data.web_discovery.extraction.max_per_day}
                </td>
              </tr>
              <tr>
                <td>Locally (free)</td>
                <td className="mono">
                  {status.data.web_discovery.extraction.by_method_today.LOCAL ?? 0}
                </td>
                <td className="mono detail">no cost</td>
              </tr>
              <tr>
                <td>Firecrawl fallback (paid)</td>
                <td className="mono">
                  {status.data.web_discovery.extraction.firecrawl_fallbacks_today}
                </td>
                <td className="mono detail">1 credit each</td>
              </tr>
              <tr>
                <td>Attempted, nothing usable</td>
                <td className="mono">
                  {status.data.web_discovery.extraction.local_failures_today}
                </td>
                <td className="mono detail">not retried</td>
              </tr>
            </tbody>
          </table>
          </TableWrap>
          <ul className="reason-list">
            {status.data.web_discovery.extraction.blockers.map((blocker) => (
              <li key={blocker} className="muted">
                {blocker}
              </li>
            ))}
          </ul>
        </div>
      )}

      {status.data?.web_discovery &&
        status.data.web_discovery.queries.length > 0 && (
          <div className="card">
            <h2>Discovery queries</h2>
            <p className="metric-note">
              &ldquo;Next eligible&rdquo; is a stored column, not a guess: it is
              written after every attempt — succeeded, failed or budget-refused —
              so a restart cannot reset a cooldown.
            </p>
            <TableWrap>
            <table>
              <thead>
                <tr>
                  <th>Kind</th>
                  <th>Provider</th>
                  <th>Topic</th>
                  <th>Query</th>
                  <th>Last success</th>
                  <th>Next eligible</th>
                  <th>Interval</th>
                  <th>Fails</th>
                  <th>Units</th>
                </tr>
              </thead>
              <tbody>
                {status.data.web_discovery.queries.map((row) => (
                  <tr
                    key={`${row.topic}:${row.query}`}
                    className={row.enabled ? "" : "muted"}
                  >
                    <td className="mono">{row.kind}</td>
                    <td className="mono">{row.provider}</td>
                    <td className="mono">{row.topic}</td>
                    <td>{row.query}</td>
                    <td className="mono detail">
                      {row.last_success_at
                        ? new Date(row.last_success_at).toLocaleString()
                        : "—"}
                    </td>
                    <td className="mono detail">
                      {row.enabled
                        ? row.next_eligible_at
                          ? new Date(row.next_eligible_at).toLocaleString()
                          : "now"
                        : "disabled"}
                    </td>
                    <td className="mono">{row.effective_interval_minutes}m</td>
                    <td className="mono">{row.consecutive_failures}</td>
                    <td className="mono">{row.units_used}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            </TableWrap>
          </div>
        )}

      <div className="grid">
        <div className="card">
          <h2>Sources by provider</h2>
          <TableWrap>
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
          </TableWrap>
        </div>

        <div className="card">
          <h2>Events by status</h2>
          <TableWrap>
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
          </TableWrap>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 18 }}>
        <h2>Scheduled tasks</h2>
        <TableWrap>
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
        </TableWrap>
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
                every {topic.interval_minutes}m · last {topic.freshness_days}d · limit{" "}
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
                    {query.kind} · {query.provider} · {query.results_seen} results ·{" "}
                    {query.units_used} units
                    {query.consecutive_failures > 0 &&
                      ` · ${query.consecutive_failures} failures`}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ))}
        {topics.data && topics.data.length === 0 && (
          <EmptyState title="No discovery topics configured">
            <p>
              Topics are seeded on first start and stored in the database. An
              empty list means the seed has not run or the rows were removed;
              scheduled web discovery has nothing to search for until one exists.
            </p>
          </EmptyState>
        )}
      </div>
    </>
  );
}
