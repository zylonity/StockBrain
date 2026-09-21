import { useCallback, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type {
  EventListResponse,
  EventStatus,
  SourceProvider,
} from "../api/types";
import { CategoryBadge, EventStatusBadge, ProviderBadge } from "../components/Badges";
import { Async, EmptyState, PageHeader, RefreshButton, TableWrap } from "../components/Page";
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

// Every provider that can appear on a source row. BRAVE and EXA were missing,
// so two of the three live discovery backends could not be filtered on at all.
// FIRECRAWL is kept because rows it discovered before the provider split still
// exist and are still valid evidence. The five disclosure feeds were missing
// next -- same bug, same consequence -- so this array is the dropdown's copy of
// the server's `SourceProvider` enum and has to stay in step with it.
const PROVIDERS: SourceProvider[] = [
  "ALPACA",
  "BRAVE",
  "EXA",
  "FIRECRAWL",
  "SEC",
  "MANUAL",
  "INVESTEGATE",
  "EQS",
  "CNMV",
  "GLOBENEWSWIRE",
  "ACTUSNEWS",
];

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

  const filtering = Boolean(search || status || provider || sinceHours);

  return (
    <>
      <PageHeader
        title="Events"
        subtitle="Canonical events, deduplicated from every discovery source. Several articles about one story appear here once, with all of their evidence attached."
        actions={<RefreshButton onClick={refresh} busy={loading} />}
      />

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

      <Async
        state={{ data, error, loading, refresh }}
        errorTitle="Events unavailable"
        rows={6}
        empty={(payload) =>
          payload.events.length === 0 ? (
            <EmptyState
              title={filtering ? "No events match these filters" : "No events ingested yet"}
              actions={
                filtering ? (
                  <button
                    type="button"
                    onClick={() => {
                      setSearch("");
                      setStatus("");
                      setProvider("");
                      setSinceHours("");
                      setOffset(0);
                    }}
                  >
                    Clear filters
                  </button>
                ) : (
                  <Link className="button-quiet" to="/discovery">
                    Discovery status
                  </Link>
                )
              }
            >
              <p>
                {filtering
                  ? "Try widening the time window, or clearing the status and provider filters."
                  : "Discovery providers need credentials before news, filings or searches arrive. The Discovery page shows which are configured and what each is allowed to spend."}
              </p>
            </EmptyState>
          ) : null
        }
      >
        {() => (
          <>
      <div className="card card-table">
        <TableWrap>
        <table>
          <thead>
            <tr>
              <th>Event</th>
              <th className="tight">Status</th>
              <th className="num">Sources</th>
              <th className="num">Companies</th>
              <th className="num">Importance</th>
              <th className="tight">First seen</th>
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
                    {event.event_type && (
                      <span className="badge">{event.event_type}</span>
                    )}
                    {event.providers.map((name) => (
                      <ProviderBadge key={name} provider={name} />
                    ))}
                  </div>
                </td>
                <td>
                  <EventStatusBadge status={event.status} />
                </td>
                <td className="num">{event.source_count}</td>
                <td className="num">{event.company_count || "—"}</td>
                <td className="num">{formatScore(event.importance_score)}</td>
                <td className="tight detail" title={formatTimestamp(event.first_seen_at)}>
                  {formatRelative(event.first_seen_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </TableWrap>
      </div>

      <div className="pager">
        <span className="pager-count">
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
        )}
      </Async>
    </>
  );
}
