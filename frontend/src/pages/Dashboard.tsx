import { useMemo } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type {
  DiscoveryStatus,
  EventListResponse,
  ExecutionStatusSummary,
  HealthResponse,
  PortfolioResponse,
  ProposalListResponse,
  ProvidersResponse,
  ReadinessResponse,
  ResearchRun,
} from "../api/types";
import { EventStatusBadge, ProviderBadge } from "../components/Badges";
import { ControlBanner } from "../components/ControlBanner";
import { ExecutionBanner } from "../components/ExecutionBanner";
import { EmptyState, PageHeader, RefreshButton, TableWrap } from "../components/Page";
import { StatusPill } from "../components/StatusPill";
import {
  formatDuration,
  formatMoney,
  formatQuantity,
  formatRelative,
  formatUsd,
  isNegative,
} from "../components/formats";
import { usePolling } from "../components/usePolling";

/**
 * What an operator needs before deciding whether today needs their attention.
 *
 * Organised around one question -- "is anything waiting for me, and is anything
 * wrong?" -- rather than around the system's own structure. So the first screen
 * is: what needs a decision, what is broken, what is it costing, and what has
 * it found. Each panel is a summary with a link to the page that owns the
 * detail; none of them re-implements a specialist page.
 *
 * Everything is read from the API and never recomputed in the browser.
 */

const REFRESH_MS = 20_000;

/** Stable references so `usePolling` does not rebuild a fetcher every render. */
const recentEvents = () => api.events({ limit: 6 });
const activeProposals = () => api.proposals(undefined, 50);
const recentResearch = () => api.research();

export function Dashboard() {
  const health = usePolling<HealthResponse>(api.health, REFRESH_MS);
  const readiness = usePolling<ReadinessResponse>(api.readiness, REFRESH_MS);
  const providers = usePolling<ProvidersResponse>(api.providers, REFRESH_MS);
  const execution = usePolling<ExecutionStatusSummary>(api.executionSummary, 30_000);
  const posture = usePolling(api.executionStatus, 120_000);
  // Polled faster than the execution posture: a halt can be engaged from
  // Telegram at any moment, and a stale banner is the one that matters.
  const control = usePolling(api.controlState, 15_000);
  const discovery = usePolling<DiscoveryStatus>(api.discoveryStatus, REFRESH_MS);
  const proposals = usePolling<ProposalListResponse>(activeProposals, REFRESH_MS);
  const research = usePolling<ResearchRun[]>(recentResearch, 60_000);
  const events = usePolling<EventListResponse>(recentEvents, REFRESH_MS);
  const portfolio = usePolling<PortfolioResponse>(api.portfolio, 60_000);

  const awaiting = useMemo(
    () =>
      (proposals.data?.items ?? []).filter((proposal) =>
        ["READY", "NOTIFIED", "APPROVAL_PENDING"].includes(proposal.status),
      ),
    [proposals.data],
  );

  const troubled = useMemo(
    () =>
      (providers.data?.providers ?? []).filter((provider) =>
        ["DOWN", "DEGRADED", "BUDGET_EXHAUSTED"].includes(provider.status),
      ),
    [providers.data],
  );

  const runningResearch = useMemo(
    () => (research.data ?? []).filter((run) => ["PENDING", "RUNNING"].includes(run.status)),
    [research.data],
  );

  const queue = discovery.data?.queue ?? null;
  const budget = discovery.data?.budget ?? null;
  const ambiguous = execution.data?.ambiguous_attempts ?? 0;

  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle="Everything below is read from the API and never recomputed in the browser."
        actions={<RefreshButton onClick={health.refresh} busy={health.loading} />}
      />

      {/* The two things that must never be discovered by navigating. */}
      {control.data && <ControlBanner control={control.data} />}
      {posture.data && <ExecutionBanner status={posture.data} />}

      {ambiguous > 0 && (
        <div className="banner banner-live">
          <div className="banner-title">
            <span>{ambiguous} order{ambiguous === 1 ? "" : "s"} in an unknown state</span>
            <StatusPill status="DOWN" label="DO NOT RESEND" />
          </div>
          <div className="banner-body">
            StockBrain transmitted a request and did not receive a definitive
            response. The order may or may not exist. Reconciliation is reading
            the broker and will not retry — do not place the trade manually until
            the outcome is known.
          </div>
          <p>
            <Link to="/proposals">Review the affected proposals →</Link>
          </p>
        </div>
      )}

      {/* ----------------------------------------------------------------
          Waiting for you
          ---------------------------------------------------------------- */}
      <div className="grid grid-tight">
        <div className="card">
          <h2>Awaiting authorization</h2>
          <div className={`metric ${awaiting.length ? "metric-warn" : ""}`}>
            {proposals.data ? awaiting.length : "—"}
          </div>
          <div className="metric-note">
            {awaiting.length > 0 ? (
              <Link to="/proposals">Review proposals →</Link>
            ) : (
              "nothing needs a decision"
            )}
          </div>
        </div>

        <div className="card">
          <h2>Providers needing attention</h2>
          <div className={`metric ${troubled.length ? "metric-warn" : "metric-ok"}`}>
            {providers.data ? troubled.length : "—"}
          </div>
          <div className="metric-note">
            {troubled.length > 0 ? (
              <Link to="/health">{troubled.map((p) => p.provider).join(", ")} →</Link>
            ) : (
              "every configured provider is healthy or deliberately off"
            )}
          </div>
        </div>

        {/* The headline number is whatever is actually wrong. A red "0 pending"
            next to "1 dead" -- which is what colouring the pending count on a
            dead job produced -- is a contradiction the reader has to resolve. */}
        <div className="card">
          <h2>Pipeline</h2>
          {queue && (queue.dead > 0 || queue.stuck > 0) ? (
            <>
              <div className="metric metric-bad">{queue.dead + queue.stuck}</div>
              <div className="metric-note">
                {queue.dead > 0 && <>{queue.dead} dead</>}
                {queue.dead > 0 && queue.stuck > 0 && " · "}
                {queue.stuck > 0 && <>{queue.stuck} stuck</>} · {queue.pending} pending ·{" "}
                <Link to="/logs?categories=jobs&min_level=warning">Job logs →</Link>
              </div>
            </>
          ) : (
            <>
              <div className="metric">{queue ? queue.pending : "—"}</div>
              <div className="metric-note">
                {queue ? (
                  <>
                    job{queue.pending === 1 ? "" : "s"} pending
                    {queue.oldest_pending_age_seconds !== null && queue.pending > 0 && (
                      <> · oldest {formatDuration(queue.oldest_pending_age_seconds)}</>
                    )}
                    {queue.running > 0 && <> · {queue.running} running</>}
                  </>
                ) : (
                  "reading"
                )}
              </div>
            </>
          )}
        </div>

        <div className="card">
          <h2>Application</h2>
          <div className="metric metric-sm">
            {health.data ? <StatusPill status={health.data.status} /> : <span className="faint">—</span>}
          </div>
          <div className="metric-note">
            {readiness.data
              ? readiness.data.ready
                ? `ready · ${health.data?.environment ?? ""}`
                : (readiness.data.detail ?? "not ready")
              : "reading"}
          </div>
        </div>
      </div>

      {/* ----------------------------------------------------------------
          Posture
          ---------------------------------------------------------------- */}
      <div className="grid">
        <div className="card">
          <h2>Execution posture</h2>
          <dl className="kv">
            <dt>Environment</dt>
            <dd>
              {execution.data ? (
                <span
                  className={`pill ${
                    execution.data.broker_environment === "live" ? "pill-down" : "pill-disabled"
                  }`}
                >
                  {execution.data.broker_environment === "live"
                    ? "LIVE — REAL MONEY"
                    : `${execution.data.broker_environment.toUpperCase()} (paper)`}
                </span>
              ) : (
                "—"
              )}
            </dd>
            <dt>Authorization</dt>
            <dd>{execution.data?.execution_policy ?? "—"}</dd>
            <dt>Transmission</dt>
            <dd>
              {execution.data
                ? execution.data.order_transmission_permitted
                  ? "permitted"
                  : "blocked"
                : "—"}
            </dd>
            <dt>Awaiting reconciliation</dt>
            <dd className={execution.data?.reconciliation_pending ? "metric-warn" : ""}>
              {execution.data?.reconciliation_pending ?? "—"}
            </dd>
          </dl>
          {execution.data && execution.data.blockers.length > 0 && (
            <ul className="reason-list">
              {execution.data.blockers.slice(0, 3).map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          )}
          <p className="metric-note">
            <Link to="/health">Health and controls →</Link>
          </p>
        </div>

        <div className="card">
          <h2>Account</h2>
          {portfolio.data?.available ? (
            <>
              <div className="metric">
                {formatMoney(portfolio.data.total_value, portfolio.data.currency)}
              </div>
              <div className="metric-note">
                <span className={isNegative(portfolio.data.result_value) ? "metric-bad" : "metric-ok"}>
                  {formatMoney(portfolio.data.result_value, portfolio.data.currency)}
                </span>{" "}
                unrealised · {portfolio.data.position_count} position
                {portfolio.data.position_count === 1 ? "" : "s"} ·{" "}
                {formatMoney(portfolio.data.cash_available, portfolio.data.currency)} cash
              </div>
              <div className="metric-note">
                {portfolio.data.stale ? (
                  <span className="metric-warn">
                    snapshot is stale ({formatRelative(portfolio.data.captured_at)})
                  </span>
                ) : (
                  <>captured {formatRelative(portfolio.data.captured_at)}</>
                )}{" "}
                · <Link to="/portfolio">Positions →</Link>
              </div>
            </>
          ) : (
            <>
              <div className="metric metric-sm faint">no snapshot</div>
              <div className="metric-note">
                {portfolio.data?.reason ?? "reading"}{" "}
                <Link to="/portfolio">Portfolio →</Link>
              </div>
            </>
          )}
        </div>

        <div className="card">
          <h2>Model spend</h2>
          {budget ? (
            <>
              <div
                className={`metric ${
                  budget.status === "HARD_EXCEEDED"
                    ? "metric-bad"
                    : budget.status === "SOFT_EXCEEDED"
                      ? "metric-warn"
                      : ""
                }`}
              >
                {formatUsd(budget.daily_spend_usd, 2)}
              </div>
              <Meter
                used={Number(budget.daily_spend_usd)}
                limit={Number(budget.daily_hard_usd)}
                soft={Number(budget.daily_soft_usd)}
              />
              <div className="metric-note">
                today of {formatUsd(budget.daily_hard_usd, 2)} hard cap ·{" "}
                {formatUsd(budget.monthly_spend_usd, 2)} this month of{" "}
                {formatUsd(budget.monthly_hard_usd, 2)}
              </div>
              {budget.reason && <div className="metric-note metric-warn">{budget.reason}</div>}
            </>
          ) : (
            <div className="metric-note">
              No budget guard is running. <Link to="/discovery">Discovery →</Link>
            </div>
          )}
        </div>

        <div className="card">
          <h2>Discovery today</h2>
          <div className="metric">{discovery.data?.stats.events_last_24h ?? "—"}</div>
          <div className="metric-note">
            events in 24h · {discovery.data?.stats.sources_last_24h ?? "—"} sources · last{" "}
            {formatRelative(discovery.data?.stats.latest_source_at)}
          </div>
          <div className="metric-note">
            {discovery.data?.paused ? (
              <span className="metric-warn">discovery is held</span>
            ) : discovery.data?.discovery_enabled === false ? (
              <span className="faint">discovery is switched off</span>
            ) : null}{" "}
            <Link to="/discovery">Topics and budgets →</Link>
          </div>
        </div>
      </div>

      {/* ----------------------------------------------------------------
          What the system has found
          ---------------------------------------------------------------- */}
      <div className="grid grid-wide">
        <div className="card card-table">
          <div className="card-head">
            <h2>Latest events</h2>
            <Link className="button-quiet" to="/events">
              All events →
            </Link>
          </div>
          {events.data && events.data.events.length === 0 ? (
            <div style={{ padding: "0 16px 14px" }}>
              <EmptyState title="Nothing ingested yet">
                <p>
                  Discovery providers need credentials before news, filings or
                  searches arrive.
                </p>
              </EmptyState>
            </div>
          ) : (
            <TableWrap>
              <table className="stack-narrow">
                <tbody>
                  {(events.data?.events ?? []).map((event) => (
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
                      <td className="tight">
                        <EventStatusBadge status={event.status} />
                      </td>
                      <td className="tight detail nowrap">
                        {formatRelative(event.first_seen_at)}
                      </td>
                    </tr>
                  ))}
                  {!events.data &&
                    Array.from({ length: 3 }, (_, index) => (
                      <tr key={index}>
                        <td colSpan={3}>
                          <div className="skeleton" aria-hidden="true" />
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </TableWrap>
          )}
        </div>

        <div className="card card-table">
          <div className="card-head">
            <h2>Research and proposals</h2>
            <Link className="button-quiet" to="/research">
              All research →
            </Link>
          </div>
          <div style={{ padding: "0 16px 4px" }}>
            <dl className="kv">
              <dt>Research running</dt>
              <dd>{research.data ? runningResearch.length : "—"}</dd>
              <dt>Research runs held</dt>
              <dd>{research.data ? research.data.length : "—"}</dd>
              <dt>Proposals awaiting</dt>
              <dd>{proposals.data ? awaiting.length : "—"}</dd>
              <dt>Proposals by status</dt>
              <dd>
                {proposals.data
                  ? Object.entries(proposals.data.counts_by_status)
                      .map(([status, count]) => `${status} ${count}`)
                      .join(" · ") || "none"
                  : "—"}
              </dd>
            </dl>
          </div>
          {awaiting.length > 0 && (
            <TableWrap>
              <table>
                <tbody>
                  {awaiting.slice(0, 5).map((proposal) => (
                    <tr key={proposal.id}>
                      <td>
                        <Link className="event-title" to={`/proposals/${proposal.id}`}>
                          {proposal.side} {formatQuantity(proposal.proposed_quantity)}{" "}
                          {proposal.company_name ?? proposal.broker_ticker}
                        </Link>
                        <div className="detail mono">
                          {formatMoney(proposal.estimated_notional, proposal.account_currency)}
                        </div>
                      </td>
                      <td className="tight detail nowrap">
                        expires {formatRelative(proposal.expires_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}
        </div>
      </div>
    </>
  );
}

/**
 * A consumed allowance.
 *
 * Amber past the soft limit and red at the ceiling: a bar that is only ever one
 * colour makes "nearly out" and "plenty left" look identical at a glance, which
 * is the exact failure a spend meter exists to prevent.
 */
function Meter({ used, limit, soft }: { used: number; limit: number; soft: number }) {
  if (!Number.isFinite(limit) || limit <= 0) return null;
  const fraction = Math.max(0, Math.min(1, used / limit));
  const tone = used >= limit ? "meter-bad" : used >= soft ? "meter-warn" : "";
  return (
    <div className={`meter ${tone}`} role="presentation">
      <div className="meter-fill" style={{ width: `${fraction * 100}%` }} />
    </div>
  );
}
