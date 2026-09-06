import { useCallback, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import type { LogEntry, LogFacets, LogQueryResponse } from "../api/types";
import { Async, EmptyState, PageHeader, RefreshButton, TableWrap } from "../components/Page";
import { formatClock, formatRelative, formatTimestamp } from "../components/formats";
import { usePolling } from "../components/usePolling";

/**
 * The application's own structured logs, filterable.
 *
 * This page exists because "what is StockBrain doing right now" was only
 * answerable from a terminal, and the system runs unattended on a NAS. Every
 * filter is a URL parameter, which is what makes the health board's
 * "View logs" action a link rather than a feature: a DOWN provider links
 * straight to `?services=<that provider>`.
 *
 * Two honesty requirements shape the layout:
 *
 * The buffer is bounded and in memory. The footer always says what it holds,
 * since when, and how many entries have been evicted -- a page that silently
 * showed the last four thousand lines of a longer incident would be worse than
 * one that says so.
 *
 * The severity floor is the process's own log level. A `min_level` filter can
 * only narrow below what was captured, so a deployment running at WARNING is
 * told that INFO was never recorded rather than being shown an empty table.
 */

const REFRESH_MS = 10_000;
const PAGE_SIZE = 100;

const LEVELS: { value: string; label: string }[] = [
  { value: "", label: "All levels" },
  { value: "debug", label: "Debug and above" },
  { value: "info", label: "Info and above" },
  { value: "warning", label: "Warning and above" },
  { value: "error", label: "Errors only" },
];

const WINDOWS: { value: string; label: string }[] = [
  { value: "", label: "Everything held" },
  { value: "15", label: "Last 15 minutes" },
  { value: "60", label: "Last hour" },
  { value: "360", label: "Last 6 hours" },
  { value: "1440", label: "Last 24 hours" },
];

export function Logs() {
  const [params, setParams] = useSearchParams();
  const [offset, setOffset] = useState(0);

  const service = params.get("services") ?? "";
  const category = params.get("categories") ?? "";
  const level = params.get("min_level") ?? "";
  const since = params.get("since_minutes") ?? "";
  const search = params.get("search") ?? "";

  /**
   * Filters live in the URL, not in component state.
   *
   * That is what makes a filtered view shareable, bookmarkable, and reachable
   * from another page -- and it means the back button undoes a filter, which is
   * what an operator expects it to do.
   */
  const setFilter = useCallback(
    (key: string, value: string) => {
      const next = new URLSearchParams(params);
      if (value) next.set(key, value);
      else next.delete(key);
      setParams(next, { replace: true });
      setOffset(0);
    },
    [params, setParams],
  );

  const clearFilters = useCallback(() => {
    setParams(new URLSearchParams(), { replace: true });
    setOffset(0);
  }, [setParams]);

  const fetcher = useCallback(
    () =>
      api.logs({
        ...(level ? { minLevel: level } : {}),
        ...(service ? { services: service.split(",") } : {}),
        ...(category ? { categories: category.split(",") } : {}),
        ...(since ? { sinceMinutes: Number(since) } : {}),
        ...(search ? { search } : {}),
        limit: PAGE_SIZE,
        offset,
      }),
    [level, service, category, since, search, offset],
  );

  const logs = usePolling<LogQueryResponse>(fetcher, REFRESH_MS);
  const facets = usePolling<LogFacets>(api.logFacets, 60_000);

  const filtered = Boolean(service || category || level || since || search);

  const serviceOptions = useMemo(() => {
    const counts = facets.data?.services ?? {};
    const names = Object.keys(counts).sort();
    // A service named in the URL but absent from the buffer must still appear,
    // or arriving from a health-board link would silently drop the filter.
    if (service && !names.includes(service)) names.unshift(service);
    return names.map((name) => ({
      value: name,
      label: counts[name] ? `${name} (${counts[name]})` : name,
    }));
  }, [facets.data, service]);

  const categoryOptions = useMemo(() => {
    const counts = facets.data?.categories ?? {};
    const names = Object.keys(counts).sort();
    if (category && !names.includes(category)) names.unshift(category);
    return names.map((name) => ({
      value: name,
      label: counts[name] ? `${name} (${counts[name]})` : name,
    }));
  }, [facets.data, category]);

  return (
    <>
      <PageHeader
        title="Logs"
        subtitle="Structured events from the running application, filterable by service, severity, category and time. Credentials are removed before an entry is stored, never before it is shown."
        actions={<RefreshButton onClick={logs.refresh} busy={logs.loading} />}
      />

      <div className="filters">
        <input
          type="search"
          placeholder="Search events, loggers and field values…"
          aria-label="Search log entries"
          defaultValue={search}
          onChange={(event) => setFilter("search", event.target.value)}
        />
        <select
          aria-label="Filter by service"
          value={service}
          onChange={(event) => setFilter("services", event.target.value)}
        >
          <option value="">All services</option>
          {serviceOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter by category"
          value={category}
          onChange={(event) => setFilter("categories", event.target.value)}
        >
          <option value="">All categories</option>
          {categoryOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter by severity"
          value={level}
          onChange={(event) => setFilter("min_level", event.target.value)}
        >
          {LEVELS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter by time"
          value={since}
          onChange={(event) => setFilter("since_minutes", event.target.value)}
        >
          {WINDOWS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        {filtered && (
          <button type="button" onClick={clearFilters}>
            Clear filters
          </button>
        )}
      </div>

      <Async state={logs} errorTitle="Logs unavailable" rows={8}>
        {(data) => (
          <>
            {!data.enabled && (
              <div className="banner banner-warn">
                <div className="banner-title">Log capture is switched off</div>
                <div className="banner-body">
                  <code>LOG_BUFFER_SIZE</code> is 0, so nothing is being held for
                  this page. The application still writes to standard output.
                </div>
              </div>
            )}

            <div className="filter-summary">
              <span>
                {data.total.toLocaleString()} matching {data.total === 1 ? "entry" : "entries"}
                {filtered ? " for these filters" : ""}
              </span>
              <span className="faint">
                {data.stored.toLocaleString()} of {data.capacity.toLocaleString()} held ·{" "}
                capturing {data.min_captured_level} and above
              </span>
            </div>

            {data.entries.length === 0 ? (
              <EmptyState
                title={filtered ? "Nothing matches these filters" : "No log entries yet"}
                actions={
                  filtered ? (
                    <button type="button" onClick={clearFilters}>
                      Clear filters
                    </button>
                  ) : undefined
                }
              >
                <p>
                  {filtered
                    ? `The buffer holds ${data.stored.toLocaleString()} entries from ${formatRelative(
                        data.oldest_at,
                      )} onwards. Try widening the time window or lowering the severity.`
                    : `The buffer is in memory and starts empty after a restart. It has been capturing since ${formatTimestamp(
                        data.captured_since,
                      )}.`}
                </p>
              </EmptyState>
            ) : (
              <div className="card card-table">
                <TableWrap>
                  <table className="log-table">
                    <thead>
                      <tr>
                        <th className="tight">Time</th>
                        <th className="tight">Level</th>
                        <th className="tight">Service</th>
                        <th>Event</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.entries.map((entry) => (
                        <LogRow key={entry.sequence} entry={entry} onFilter={setFilter} />
                      ))}
                    </tbody>
                  </table>
                </TableWrap>
              </div>
            )}

            <div className="pager">
              <span className="pager-count">
                {data.total > 0 && (
                  <>
                    showing {data.offset + 1}–
                    {Math.min(data.offset + data.entries.length, data.total)} of{" "}
                    {data.total.toLocaleString()}
                  </>
                )}
              </span>
              <button
                type="button"
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                disabled={offset === 0}
              >
                Newer
              </button>
              <button
                type="button"
                onClick={() => setOffset(offset + PAGE_SIZE)}
                disabled={offset + PAGE_SIZE >= data.total}
              >
                Older
              </button>
            </div>

            <p className="detail">
              Held in memory only — {data.stored.toLocaleString()} of{" "}
              {data.capacity.toLocaleString()} entries, capturing since{" "}
              {formatTimestamp(data.captured_since)}
              {data.dropped > 0 && (
                <>
                  {" "}
                  · {data.dropped.toLocaleString()} older{" "}
                  {data.dropped === 1 ? "entry has" : "entries have"} been evicted
                </>
              )}
              . A restart clears it; raise <code>LOG_BUFFER_SIZE</code> to hold more.
            </p>
          </>
        )}
      </Async>
    </>
  );
}

function LogRow({
  entry,
  onFilter,
}: {
  entry: LogEntry;
  onFilter: (key: string, value: string) => void;
}) {
  const fields = Object.entries(entry.fields);
  const level = entry.level.toLowerCase();
  return (
    <tr className={`log-row-${level}`}>
      <td className="mono detail tight" title={formatTimestamp(entry.timestamp)}>
        {formatClock(entry.timestamp)}
      </td>
      <td className="tight">
        <span className={`badge badge-level-${level}`}>{level}</span>
      </td>
      <td className="tight">
        {/* Clicking a service narrows to it, which is how an operator follows a
            thread out of a mixed stream without retyping a filter. */}
        <button
          type="button"
          className="button-quiet mono"
          onClick={() => onFilter("services", entry.service)}
          title={`Show only ${entry.service}`}
        >
          {entry.service}
        </button>
      </td>
      <td>
        <span className="log-event">{entry.event}</span>
        {fields.length > 0 && (
          <div className="log-fields">
            {fields.map(([key, value]) => (
              <span key={key}>
                <span className="log-field-key">{key}=</span>
                {value}
              </span>
            ))}
          </div>
        )}
        {entry.message && <pre className="log-exception">{entry.message}</pre>}
      </td>
    </tr>
  );
}
