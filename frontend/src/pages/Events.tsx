import { useCallback, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type {
  EventListResponse,
  EventStatus,
  SourceProvider,
} from "../api/types";
import { CategoryBadge, EventStatusBadge, ProviderBadge } from "../components/Badges";
import { formatRelative, formatScore, formatTimestamp } from "../components/formats";
import { usePolling } from "../components/usePolling";

const PAGE_SIZE = 25;

const STATUSES: EventStatus[] = [
  "NEW",
  "CLASSIFYING",
  "CLASSIFIED",
  "CLASSIFICATION_FAILED",
  "IRRELEVANT",
  "CANDIDATE",
  "RESEARCHING",
  "RESEARCHED",
  "ARCHIVED",
];

const PROVIDERS: SourceProvider[] = ["ALPACA", "FIRECRAWL", "SEC", "MANUAL"];

export function Events() {
  const [status, setStatus] = useState<EventStatus | "">("");
  const [provider, setProvider] = useState<SourceProvider | "">("");
  const [search, setSearch] = useState("");
  const [sinceHours, setSinceHours] = useState<number | "">("");
  const [offset, setOffset] = useState(0);

  // Filtering is done by the API, not in the browser: the server is the source
  // of truth and the client must not hold a second, divergent copy of the rules.
  const fetcher = useCallback(
    () =>
      api.events({
        ...(status ? { status } : {}),
        ...(provider ? { provider } : {}),
        ...(search ? { search } : {}),
        ...(sinceHours ? { sinceHours } : {}),
        limit: PAGE_SIZE,
        offset,
      }),
    [status, provider, search, sinceHours, offset],
  );

  const { data, error, loading, refresh } = usePolling<EventListResponse>(
    fetcher,
    20_000,
  );

  const total = data?.total ?? 0;
  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const resetTo = useMemo(
    () =>
      <T,>(setter: (value: T) => void) =>
        (value: T) => {
          setter(value);
          setOffset(0);
        },
    [],
  );

  return (
    <>
      <div className="refresh-row">
        <div>
          <h1 className="page-title">Events</h1>
          <p className="page-subtitle" style={{ marginBottom: 0 }}>
            Canonical events, deduplicated from every discovery source. Several
            articles about one story appear here once, with all of their evidence
            attached.
          </p>
        </div>
        <button onClick={refresh} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      <div className="filters">
        <input
          type="search"
          placeholder="Search titles…"
          value={search}
          onChange={(e) => resetTo(setSearch)(e.target.value)}
          aria-label="Search event titles"
        />
        <select
          value={status}
          onChange={(e) => resetTo(setStatus)(e.target.value as EventStatus | "")}
          aria-label="Filter by status"
        >
          <option value="">All statuses</option>
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
        <select
          value={provider}
          onChange={(e) =>
            resetTo(setProvider)(e.target.value as SourceProvider | "")
          }
          aria-label="Filter by source provider"
        >
          <option value="">All providers</option>
          {PROVIDERS.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
        <select
          value={sinceHours}
          onChange={(e) =>
            resetTo(setSinceHours)(
              e.target.value ? Number(e.target.value) : ("" as const),
            )
          }
          aria-label="Filter by age"
        >
          <option value="">Any time</option>
          <option value="1">Last hour</option>
          <option value="24">Last 24 hours</option>
          <option value="168">Last 7 days</option>
        </select>
      </div>

      {error && <div className="error">{error}</div>}

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Event</th>
              <th>Status</th>
              <th>Sources</th>
              <th>Importance</th>
              <th>First seen</th>
            </tr>
          </thead>
          <tbody>
            {(data?.events ?? []).map((event) => (
              <tr key={event.id} className="event-row">
                <td>
                  <Link className="event-title" to={`/events/${event.id}`}>
                    {event.title}
                  </Link>
                  <div className="event-meta">
                    <CategoryBadge category={event.top_category} />
                    {event.providers.map((name) => (
                      <ProviderBadge key={name} provider={name} />
                    ))}
                  </div>
                </td>
                <td>
                  <EventStatusBadge status={event.status} />
                </td>
                <td className="mono">{event.source_count}</td>
                <td className="mono">{formatScore(event.importance_score)}</td>
                <td className="mono detail" title={formatTimestamp(event.first_seen_at)}>
                  {formatRelative(event.first_seen_at)}
                </td>
              </tr>
            ))}
            {data && data.events.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  {total === 0 && !search && !status && !provider
                    ? "No events ingested yet. Discovery providers need credentials before anything arrives."
                    : "No events match these filters."}
                </td>
              </tr>
            )}
            {!data && (
              <tr>
                <td colSpan={5} className="muted">
                  Loading…
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="pager">
        <span>
          {total} event{total === 1 ? "" : "s"} · page {page} of {pages}
        </span>
        <button
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          disabled={offset === 0}
        >
          Previous
        </button>
        <button
          onClick={() => setOffset(offset + PAGE_SIZE)}
          disabled={offset + PAGE_SIZE >= total}
        >
          Next
        </button>
      </div>
    </>
  );
}
