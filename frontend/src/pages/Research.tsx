import { useCallback } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import type { ResearchRun } from "../api/types";
import {
  Async,
  EmptyState,
  ErrorState,
  LoadingRows,
  PageHeader,
  RefreshButton,
  TableWrap,
} from "../components/Page";
import { StatusPill } from "../components/StatusPill";
import { formatPercent, formatRelative, formatTimestamp, formatUsd } from "../components/formats";
import { usePolling } from "../components/usePolling";

/**
 * Multi-agent research runs, and the theses they published.
 *
 * Advisory throughout. A thesis never authorizes anything: it can only ever
 * *reduce* a size inside the deterministic limits, and it can never lift a
 * block. The confidence figure is a model ranking, not a calibrated
 * probability, and every place it is rendered says so — a bare percentage in a
 * financial interface reads as certainty unless it is told not to.
 *
 * Hidden model reasoning is not stored anywhere in StockBrain and therefore
 * cannot be displayed here. What is shown is the normalised thesis, the
 * evidence it cited, and what each call cost.
 */

const REFRESH_MS = 20_000;

/** Research status mapped onto the shared health palette. */
function statusTone(status: string) {
  if (status === "SUCCEEDED") return "HEALTHY" as const;
  if (status === "RUNNING" || status === "PENDING") return "UNKNOWN" as const;
  if (status === "CANCELLED") return "DISABLED" as const;
  if (status === "TIMED_OUT") return "DEGRADED" as const;
  return "DOWN" as const;
}

export function Research() {
  const [params] = useSearchParams();
  const eventId = params.get("event_id") ?? undefined;
  return <ResearchList key={eventId} eventId={eventId} />;
}

function ResearchList({ eventId }: { eventId: string | undefined }) {
  const fetcher = useCallback(() => api.research(eventId), [eventId]);
  const runs = usePolling<ResearchRun[]>(fetcher, REFRESH_MS);

  return (
    <>
      <PageHeader
        title="Research"
        subtitle="Event-driven analysis and the evidence behind it. Research is advisory: it can reduce a position size inside the deterministic limits and can never lift a risk block."
        actions={<RefreshButton onClick={runs.refresh} busy={runs.loading} />}
      >
        {eventId && (
          <p className="page-subtitle">
            Filtered to one event.{" "}
            <Link to="/research">Show every run →</Link>
          </p>
        )}
      </PageHeader>

      <Async state={runs} errorTitle="Research unavailable" rows={4}>
        {(data) =>
          data.length === 0 ? (
            <EmptyState
              title={eventId ? "No research for this event" : "No research runs yet"}
              actions={
                <>
                  <Link className="button-quiet" to="/events">
                    Events
                  </Link>
                  <Link className="button-quiet" to="/settings">
                    Model budgets
                  </Link>
                </>
              }
            >
              <p>
                Classified candidates enter research once their company and
                listing are resolved to a tradable broker instrument. A budget
                pause or a provider failure holds runs here rather than dropping
                them.
              </p>
            </EmptyState>
          ) : (
            <div className="card card-table">
              <TableWrap>
                <table>
                  <thead>
                    <tr>
                      <th>Company / listing</th>
                      <th>Trigger</th>
                      <th className="tight">Status</th>
                      <th className="tight">Action</th>
                      <th className="tight">As of</th>
                      <th className="num">Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.map((run) => (
                      <tr key={run.id}>
                        <td>
                          <Link className="event-title" to={`/research/${run.id}`}>
                            {run.packet?.company.name ?? "Research run"}
                          </Link>
                          <div className="detail mono">
                            {run.packet?.company.symbol ?? run.id.slice(0, 8)}
                            {run.packet?.company.exchange
                              ? ` · ${run.packet.company.exchange}`
                              : ""}
                          </div>
                        </td>
                        <td className="detail">
                          {run.packet?.title ?? (
                            <span className="faint">triggering event unavailable</span>
                          )}
                        </td>
                        <td className="tight">
                          <StatusPill status={statusTone(run.status)} label={run.status} />
                          {run.error_class && (
                            <div className="detail">{run.error_class}</div>
                          )}
                        </td>
                        <td className="tight">
                          {run.decision ? (
                            <>
                              <span className="badge badge-info">{run.decision.action}</span>
                              <div className="detail">
                                {formatPercent(run.decision.confidence)} confidence
                              </div>
                            </>
                          ) : (
                            <span className="faint">—</span>
                          )}
                        </td>
                        <td className="tight detail" title={formatTimestamp(run.as_of)}>
                          {formatRelative(run.as_of)}
                        </td>
                        <td className="num">{formatUsd(run.estimated_cost_usd)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            </div>
          )
        }
      </Async>
    </>
  );
}

/* ------------------------------------------------------------------------ */

function EvidenceLink({ url }: { url: string | null }) {
  // Only http(s), and never with a referrer or an opener back into this app:
  // an evidence URL is untrusted third-party data.
  if (!url || !/^https?:\/\//i.test(url)) return null;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer nofollow">
      Read source ↗
    </a>
  );
}

export function ResearchDetail() {
  const { runId } = useParams();
  return <ResearchRecord key={runId} runId={runId ?? ""} />;
}

function ResearchRecord({ runId }: { runId: string }) {
  const fetcher = useCallback(() => api.researchRun(runId), [runId]);
  const state = usePolling<ResearchRun>(fetcher, REFRESH_MS);
  const run = state.data;

  if (!run) {
    return (
      <>
        <Link className="back-link" to="/research">
          ← Research
        </Link>
        {state.error ? (
          <ErrorState
            title="Could not load this research run"
            error={state.error}
            onRetry={state.refresh}
          />
        ) : (
          <LoadingRows rows={4} label="Loading research run" />
        )}
      </>
    );
  }

  const packet = run.packet;
  const decision = run.decision;

  return (
    <>
      <Link className="back-link" to="/research">
        ← Research
      </Link>

      <PageHeader
        title={packet?.company.name ?? "Research run"}
        actions={<RefreshButton onClick={state.refresh} busy={state.loading} />}
      >
        <p className="page-subtitle chips">
          <StatusPill status={statusTone(run.status)} label={run.status} />
          <span className="badge">advisory</span>
          {packet && <span className="badge">{packet.company.broker_ticker}</span>}
          <span className="detail">as of {formatTimestamp(run.as_of)}</span>
        </p>
      </PageHeader>

      {run.error && (
        <div className="banner banner-warn">
          <div className="banner-title">{run.error_class ?? "Run did not complete"}</div>
          <div className="banner-body">
            {run.error}
            <p>
              Completed or interrupted paid runs are retained and are not
              automatically repeated — a retry would pay for the same call twice
              without knowing whether the first one produced anything.
            </p>
          </div>
        </div>
      )}

      {packet && (
        <div className="card">
          <h2>Trigger</h2>
          <p className="event-title">{packet.title}</p>
          <p className="research-prose">{packet.summary}</p>
          <dl className="kv">
            <dt>Listing</dt>
            <dd>
              {packet.company.symbol} · {packet.company.exchange ?? "exchange unknown"} ·{" "}
              {packet.company.currency ?? "currency unknown"}
            </dd>
            <dt>Broker ticker</dt>
            <dd>{packet.company.broker_ticker}</dd>
            <dt>ISIN</dt>
            <dd>{packet.company.isin ?? "unavailable"}</dd>
            <dt>Event time</dt>
            <dd>{formatTimestamp(packet.event_time)}</dd>
            <dt>Impact</dt>
            <dd>
              {packet.impact_path}
              {packet.relationship ? ` · ${packet.relationship}` : ""}
            </dd>
          </dl>
          {packet.classifier_rationale && (
            <p className="rationale" style={{ marginTop: 10 }}>
              {packet.classifier_rationale}
            </p>
          )}
          {run.event_id && (
            <p>
              <Link to={`/events/${run.event_id}`}>View the triggering event →</Link>
            </p>
          )}
        </div>
      )}

      {packet && packet.degradation.length > 0 && (
        <div className="card">
          <h2>Degraded inputs</h2>
          <p className="setting-description">
            The run completed without these. They are recorded rather than
            hidden: a thesis built on less evidence is not the same thesis.
          </p>
          <ul className="reason-list">
            {packet.degradation.map((item, index) => (
              <li key={index}>
                <strong>{item.provider}</strong> — {item.error_class}: {item.detail}
              </li>
            ))}
          </ul>
        </div>
      )}

      {decision && (
        <>
          <div className="card">
            <h2>
              Thesis
              <span className="badge badge-info">{decision.action}</span>
              <span className="badge">{formatPercent(decision.confidence)} confidence</span>
              <span className="badge">horizon {decision.horizon}</span>
            </h2>
            <p className="research-prose">{decision.thesis}</p>
            <p className="setting-impact">
              Confidence is a model ranking, not a calibrated probability. It may
              only reduce a size inside the deterministic limits, and can never
              lift a block.
            </p>
          </div>

          <div className="grid grid-wide">
            <div className="card">
              <h2>Bull case</h2>
              <p className="research-prose">{decision.bull_case}</p>
            </div>
            <div className="card">
              <h2>Bear case</h2>
              <p className="research-prose">{decision.bear_case}</p>
            </div>
          </div>

          <div className="grid grid-wide">
            {(
              [
                ["Catalysts", decision.catalysts],
                ["Risks", decision.risks],
                ["Invalidation conditions", decision.invalidation_conditions],
              ] as const
            ).map(([title, items]) => (
              <div className="card" key={title}>
                <h2>{title}</h2>
                {items.length > 0 ? (
                  <ul className="reason-list">
                    {items.map((item, index) => (
                      <li key={index}>{item}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="muted">None stated.</p>
                )}
              </div>
            ))}
          </div>
        </>
      )}

      {Object.keys(run.reports).length > 0 && (
        <>
          <h2 className="section-title">Analyst reports</h2>
          {Object.entries(run.reports).map(([role, report]) => (
            <details className="card" key={role}>
              <summary>
                {role === "manager" ? "Research-manager synthesis" : `${role} analysis`}
              </summary>
              <p className="research-prose">{report}</p>
            </details>
          ))}
        </>
      )}

      {packet && (
        <details className="card">
          <summary>Evidence ({packet.evidence.length})</summary>
          {packet.evidence.length === 0 ? (
            <p className="muted">No evidence was attached to this run.</p>
          ) : (
            packet.evidence.map((item) => (
              <details key={item.source_id}>
                <summary>
                  {item.publisher ?? "Unknown publisher"} ·{" "}
                  {item.published_at
                    ? formatTimestamp(item.published_at)
                    : "publication time unknown"}
                  {decision?.evidence_ids.includes(item.source_id) && (
                    <span className="badge badge-ok"> cited</span>
                  )}
                </summary>
                <p className="mono detail">{item.source_id}</p>
                <EvidenceLink url={item.url} />
                {item.text_truncated && (
                  <p className="detail">
                    Excerpt shown; the complete source is retained with the event.
                  </p>
                )}
                {/* Rendered as a text node. Source content is untrusted data and
                    is never passed to React's raw-HTML escape hatch. */}
                <p className="research-prose">{item.text}</p>
              </details>
            ))
          )}
        </details>
      )}

      {packet && packet.market_context.length > 0 && (
        <details className="card">
          <summary>Market context ({packet.market_context.length})</summary>
          {packet.market_context.map((item, index) => (
            <details key={index}>
              <summary>
                {item.provider} · {item.kind} · {formatTimestamp(item.as_of)}
              </summary>
              <pre className="research-prose">{item.text}</pre>
            </details>
          ))}
        </details>
      )}

      <details className="card">
        <summary>Model usage and versions · {formatUsd(run.estimated_cost_usd)}</summary>
        <dl className="kv">
          <dt>Quick model</dt>
          <dd>{run.quick_model ?? "—"}</dd>
          <dt>Deep model</dt>
          <dd>{run.deep_model ?? "—"}</dd>
          <dt>Prompt</dt>
          <dd>{run.prompt_version ?? "—"}</dd>
          <dt>TradingAgents</dt>
          <dd>{run.tradingagents_version ?? "—"}</dd>
          <dt>Configuration</dt>
          <dd>{run.config_version ?? "—"}</dd>
          <dt>Started</dt>
          <dd>{run.started_at ? formatTimestamp(run.started_at) : "pending"}</dd>
          <dt>Completed</dt>
          <dd>{run.completed_at ? formatTimestamp(run.completed_at) : "pending"}</dd>
        </dl>
        <TableWrap>
          <table>
            <thead>
              <tr>
                <th>Role / model</th>
                <th className="num">In / out</th>
                <th className="num">Cache hit / miss</th>
                <th className="num">Latency</th>
                <th className="num">Cost</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              {run.calls.map((call) => (
                <tr key={call.id}>
                  <td>
                    {call.purpose}
                    <div className="detail mono">
                      {call.provider} · {call.model}
                    </div>
                  </td>
                  <td className="num">
                    {call.input_tokens ?? 0} / {call.output_tokens ?? 0}
                  </td>
                  <td className="num">
                    {call.cached_input_tokens ?? 0} / {call.cache_miss_input_tokens ?? 0}
                  </td>
                  <td className="num">{call.latency_ms ?? 0} ms</td>
                  <td className="num">{formatUsd(call.estimated_cost_usd)}</td>
                  <td className="detail">
                    {call.error_class ? (
                      <span className="badge badge-bad">{call.error_class}</span>
                    ) : (
                      call.finish_reason
                    )}
                    <div className="faint mono">{call.provider_request_id}</div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
        <p className="detail">
          Cost is estimated from recorded token counts and configured rates, for
          telemetry only. Hidden model reasoning is never stored or displayed.
        </p>
      </details>
    </>
  );
}
