import { useCallback } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "../api/client";
import type { EventDetail as EventDetailPayload } from "../api/types";
import { CategoryBadge, EventStatusBadge, ProviderBadge } from "../components/Badges";
import { formatScore, formatTimestamp, hostOf } from "../components/formats";
import { usePolling } from "../components/usePolling";

export function EventDetail() {
  const { eventId } = useParams<{ eventId: string }>();

  const fetcher = useCallback(
    () => api.event(eventId ?? ""),
    [eventId],
  );
  const { data, error, loading, refresh } = usePolling<EventDetailPayload>(
    fetcher,
    30_000,
  );

  return (
    <>
      <Link className="back-link" to="/events">
        ← All events
      </Link>

      {error && <div className="error">{error}</div>}
      {!data && !error && <div className="placeholder">Loading…</div>}

      {data && (
        <>
          <div className="refresh-row">
            <div>
              <h1 className="page-title">{data.event.title}</h1>
              <div className="event-meta">
                <EventStatusBadge status={data.event.status} />
                <CategoryBadge category={data.event.top_category} />
                {data.event.providers.map((name) => (
                  <ProviderBadge key={name} provider={name} />
                ))}
              </div>
            </div>
            <button onClick={refresh} disabled={loading}>
              {loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>

          <div className="grid">
            <div className="card">
              <h2>Timing</h2>
              <dl className="kv">
                <dt>First seen</dt>
                <dd>{formatTimestamp(data.event.first_seen_at)}</dd>
                <dt>Event time</dt>
                <dd>{formatTimestamp(data.event.event_time)}</dd>
              </dl>
            </div>
            <div className="card">
              <h2>Classification</h2>
              <dl className="kv">
                <dt>Type</dt>
                <dd>{data.event.event_type ?? "—"}</dd>
                <dt>Importance</dt>
                <dd>{formatScore(data.event.importance_score)}</dd>
                <dt>Novelty</dt>
                <dd>{formatScore(data.event.novelty_score)}</dd>
              </dl>
              {data.event.status === "NEW" && (
                <div className="metric-note">
                  Awaiting classification. The scoring model lands in the next
                  phase.
                </div>
              )}
            </div>
            <div className="card">
              <h2>Evidence</h2>
              <div className="metric">{data.sources.length}</div>
              <div className="metric-note">
                {data.sources.length === 1
                  ? "single source"
                  : "sources describing this event"}
              </div>
            </div>
          </div>

          {data.event.summary && (
            <div className="card" style={{ marginBottom: 18 }}>
              <h2>Summary</h2>
              <p style={{ margin: 0 }}>{data.event.summary}</p>
            </div>
          )}

          <h2 className="page-title" style={{ fontSize: 15, marginBottom: 10 }}>
            Sources
          </h2>
          {data.sources.map((source) => (
            <div className="source" key={source.id}>
              <div className="source-head">
                <span className="source-headline">
                  {source.headline ?? "(no headline)"}
                </span>
                <ProviderBadge provider={source.provider} />
                <CategoryBadge category={source.source_category} />
                {source.relationship && (
                  <span className="badge">{source.relationship}</span>
                )}
              </div>

              <dl className="kv">
                <dt>Publisher</dt>
                <dd>
                  {source.source_name ?? hostOf(source.canonical_url)}
                  {source.author ? ` · ${source.author}` : ""}
                </dd>
                <dt>Published</dt>
                <dd>{formatTimestamp(source.published_at)}</dd>
                <dt>Ingested</dt>
                <dd>{formatTimestamp(source.received_at)}</dd>
                {source.canonical_url && (
                  <>
                    <dt>Link</dt>
                    <dd>
                      {/* External, untrusted destination: no referrer, no
                          window.opener access back into this app. */}
                      <a
                        href={source.canonical_url}
                        target="_blank"
                        rel="noopener noreferrer nofollow"
                      >
                        {source.canonical_url}
                      </a>
                    </dd>
                  </>
                )}
                {source.symbols.length > 0 && (
                  <>
                    <dt>Symbol hints</dt>
                    <dd>{source.symbols.join(", ")}</dd>
                  </>
                )}
              </dl>

              {source.excerpt && (
                // Rendered as a text node. Source content is untrusted data and
                // is never passed to React's raw-HTML escape hatch anywhere in
                // this codebase.
                <div className="source-excerpt">{source.excerpt}</div>
              )}
            </div>
          ))}
        </>
      )}
    </>
  );
}
