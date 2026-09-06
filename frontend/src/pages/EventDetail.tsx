import { useCallback } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "../api/client";
import type { EventDetail as EventDetailPayload } from "../api/types";
import { CategoryBadge, EventStatusBadge, ProviderBadge } from "../components/Badges";
import {
  ClassificationPanel,
  CompanyImpactTable,
  LlmUsagePanel,
} from "../components/Classification";
import { formatTimestamp, hostOf } from "../components/formats";
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

      <p><Link to={`/research?event_id=${encodeURIComponent(eventId ?? "")}`}>Research for this event</Link></p>

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
                {data.event.event_type && (
                  <span className="badge">{data.event.event_type}</span>
                )}
                {data.event.providers.map((name) => (
                  <ProviderBadge key={name} provider={name} />
                ))}
                {data.event.needs_corroboration && (
                  <span className="badge badge-cat-UNKNOWN">
                    needs corroboration
                  </span>
                )}
              </div>
              {data.event.topics.length > 0 && (
                <div className="topic-chips">
                  {data.event.topics.map((topic) => (
                    <span className="badge" key={topic}>
                      {topic}
                    </span>
                  ))}
                </div>
              )}
            </div>
            <button onClick={refresh} disabled={loading}>
              {loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>

          {data.event.merged_into_event_id && (
            <div className="banner banner-warn">
              <div className="banner-title">Merged into another event</div>
              <div className="banner-body">
                Semantic deduplication folded this event into{" "}
                <Link to={`/events/${data.event.merged_into_event_id}`}>
                  the surviving event
                </Link>
                . This record is kept so the merge stays auditable.
              </div>
            </div>
          )}

          <div className="grid">
            <ClassificationPanel event={data.event} />
            <div className="card">
              <h2>Timing</h2>
              <dl className="kv">
                <dt>First seen</dt>
                <dd>{formatTimestamp(data.event.first_seen_at)}</dd>
                <dt>Event time</dt>
                <dd>{formatTimestamp(data.event.event_time)}</dd>
                <dt>Sources</dt>
                <dd>{data.sources.length}</dd>
                <dt>Companies</dt>
                <dd>{data.event.company_count}</dd>
              </dl>
            </div>
            <LlmUsagePanel usage={data.llm_usage} calls={data.llm_calls} />
          </div>

          {data.event.summary && (
            <div className="card" style={{ marginBottom: 18 }}>
              <h2>Summary</h2>
              <p style={{ margin: 0 }}>{data.event.summary}</p>
            </div>
          )}

          {data.rationale && (
            <div className="card" style={{ marginBottom: 18 }}>
              <h2>Classifier rationale</h2>
              <p className="rationale">{data.rationale}</p>
            </div>
          )}

          <div className="card" style={{ marginBottom: 18 }}>
            <h2>Affected companies</h2>
            <CompanyImpactTable companies={data.companies} />
          </div>

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
                {/* Every provider *after* the first that surfaced this exact
                    page. Two providers finding one article is corroboration
                    and is worth seeing; it is not two events. */}
                {source.discovered_by
                  .filter((name) => name !== source.provider)
                  .map((name) => (
                    <span className="badge" key={name} title="also found by">
                      + {name}
                    </span>
                  ))}
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
                <dt>Body</dt>
                <dd>
                  {source.content_fetched_at
                    ? `${source.extraction_method ?? "unknown"} · ${formatTimestamp(
                        source.content_fetched_at,
                      )}`
                    : "snippet only — not fetched"}
                </dd>
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
