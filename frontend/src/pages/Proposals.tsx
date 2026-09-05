import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, api } from "../api/client";
import type { Proposal, RiskRule } from "../api/types";
import { StatusPill } from "../components/StatusPill";
import { formatRelative, formatTimestamp } from "../components/formats";
import { ExecutionPanel } from "../components/ExecutionPanel";
import { usePolling } from "../components/usePolling";

/**
 * Trade proposals and the deterministic risk behind them.
 *
 * Two things this page must never let a reader assume:
 *
 * 1. **Authorization is not execution.** An APPROVED proposal means the risk
 *    engine allowed the trade and a recorded authority signed it off. No broker
 *    order exists. The banner says so on every proposal, in every state.
 * 2. **A refusal is a result, not a gap.** Blocked rules are rendered as
 *    prominently as approved ones, with the value observed beside the threshold
 *    it was compared against, because a refusal nobody can read is
 *    indistinguishable from a bug.
 *
 * Nothing here computes a number. Quantities, notionals and spreads are
 * rendered exactly as the server persisted them, so what is on screen is what
 * the decision was made on.
 */

const NO_ORDER_NOTICE =
  "Authorization records that deterministic risk allowed this trade and who signed it off. " +
  "No broker order has been sent — StockBrain has no order-submission path in this phase.";

/** Proposal status mapped onto the shared health palette. */
function statusTone(status: string) {
  if (status === "APPROVED" || status === "EXECUTED") return "HEALTHY" as const;
  if (status === "READY" || status === "NOTIFIED" || status === "APPROVAL_PENDING") {
    return "UNKNOWN" as const;
  }
  if (status === "INVALIDATED" || status === "EXPIRED" || status === "CANCELLED") {
    return "DEGRADED" as const;
  }
  if (status === "REJECTED" || status === "FAILED") return "DOWN" as const;
  return "UNKNOWN" as const;
}

function outcomeTone(outcome: string | null) {
  if (outcome === "ALLOW") return "HEALTHY" as const;
  if (outcome === "REDUCE_SIZE") return "DEGRADED" as const;
  if (outcome === "BLOCK") return "DOWN" as const;
  return "UNKNOWN" as const;
}

function ruleClass(outcome: string) {
  if (outcome === "BLOCK") return "badge badge-bad";
  if (outcome === "REDUCE" || outcome === "WARN") return "badge badge-path-indirect";
  return "badge badge-ok";
}

function PolicyBadge({ proposal }: { proposal: Proposal }) {
  const automatic = proposal.execution_policy === "AUTOMATIC";
  return (
    <span
      className={`badge ${automatic ? "badge-path-indirect" : "badge-path-direct"}`}
      title={
        automatic
          ? "Generated under the AUTOMATIC execution policy: the system may authorize it."
          : "Generated under the MANUAL execution policy: a person must authorize it."
      }
    >
      {proposal.execution_policy}
    </span>
  );
}

function Provenance({ proposal }: { proposal: Proposal }) {
  if (!proposal.authorization_source) return <span className="faint">—</span>;
  const system = proposal.authorization_source === "SYSTEM_AUTOMATIC";
  return (
    <span title={system ? "Authorized by the system, with no human in the loop." : undefined}>
      <span className={`badge ${system ? "badge-path-indirect" : "badge-ok"}`}>
        {system ? "SYSTEM-AUTHORIZED" : proposal.authorization_source}
      </span>{" "}
      <span className="detail">
        {proposal.approved_by} · {formatTimestamp(proposal.approved_at)}
      </span>
    </span>
  );
}

function ExpiryNote({ proposal }: { proposal: Proposal }) {
  const expired = new Date(proposal.expires_at).getTime() <= Date.now();
  return (
    <span className={expired ? "badge badge-bad" : "detail"}>
      {expired ? "expired " : "expires "}
      {formatRelative(proposal.expires_at)}
    </span>
  );
}

function Rules({ rules }: { rules: RiskRule[] }) {
  if (rules.length === 0) return <p className="muted">No rules were recorded.</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>Rule</th>
          <th>Outcome</th>
          <th>Observed</th>
          <th>Threshold</th>
          <th>Reason</th>
        </tr>
      </thead>
      <tbody>
        {rules.map((rule) => (
          <tr key={`${rule.rule_id}-${rule.rule_version}`}>
            <td className="mono">
              {rule.rule_id}
              <br />
              <span className="faint">v{rule.rule_version}</span>
            </td>
            <td>
              <span className={ruleClass(rule.outcome)}>{rule.outcome}</span>
            </td>
            <td className="mono">{rule.observed ?? "—"}</td>
            <td className="mono">{rule.threshold ?? "—"}</td>
            <td className="detail">{rule.reason}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Proposals() {
  const { data, error, loading, refresh } = usePolling(() => api.proposals(), 15_000);
  const policy = usePolling(() => api.proposalPolicy(), 60_000);

  return (
    <section>
      <div className="refresh-row">
        <div>
          <h1 className="page-title">Trade proposals</h1>
          <p className="page-subtitle">{NO_ORDER_NOTICE}</p>
        </div>
        <button onClick={refresh}>Refresh</button>
      </div>

      {policy.data && (
        <div
          className={`banner ${
            policy.data.automatic_authorization_permitted ? "banner-warn" : "banner-safe"
          }`}
        >
          <div className="banner-title">
            {policy.data.execution_policy === "AUTOMATIC"
              ? "AUTOMATIC authorization"
              : "MANUAL authorization"}{" "}
            · {policy.data.broker} {policy.data.broker_environment}
          </div>
          <div className="banner-body">
            {policy.data.automatic_authorization_permitted
              ? "Proposals that clear every deterministic rule are authorized by the system without a human. No broker order is sent."
              : "Every proposal requires an explicit human authorization."}
            {policy.data.automation_blockers.length > 0 && (
              <ul>
                {policy.data.automation_blockers.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            )}
            <p className="detail">
              Risk policy <span className="mono">{policy.data.risk_policy_version}</span> · TTL{" "}
              {policy.data.proposal_ttl_minutes} min · broker order routes:{" "}
              {policy.data.broker_order_routes.length === 0
                ? "none"
                : policy.data.broker_order_routes.join(", ")}
            </p>
          </div>
        </div>
      )}

      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {loading && !data && <p className="muted">Loading proposals…</p>}

      {data?.items.length === 0 && (
        <div className="card">
          <h2>No proposals yet</h2>
          <p className="muted">
            A published thesis becomes a proposal only when the deterministic risk engine allows
            it against fresh account state and a fresh execution-grade quote. Refusals are
            recorded and are visible on the risk view of the thesis that produced them.
          </p>
        </div>
      )}

      {data && data.items.length > 0 && (
        <div className="card">
          <table>
            <thead>
              <tr>
                <th>Company / listing</th>
                <th>Side / size</th>
                <th>Reference</th>
                <th>Spread</th>
                <th>Risk</th>
                <th>Policy</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((proposal) => (
                <tr key={proposal.id}>
                  <td>
                    <Link to={`/proposals/${proposal.id}`}>
                      {proposal.company_name ?? proposal.broker_ticker}
                    </Link>
                    <div className="detail">
                      <span className="mono">{proposal.broker_ticker}</span> ·{" "}
                      {proposal.exchange ?? "exchange unknown"} ·{" "}
                      {proposal.instrument_currency ?? "?"}
                    </div>
                  </td>
                  <td className="mono">
                    {proposal.side} {proposal.proposed_quantity}
                    <div className="detail">
                      {proposal.estimated_notional} {proposal.account_currency}
                    </div>
                  </td>
                  <td className="mono">
                    {proposal.reference_price}
                    <div className="detail">{proposal.quote_age_ms} ms old</div>
                  </td>
                  <td className="mono">
                    {proposal.quote_spread_bps ?? "—"} bps
                    <div className="detail">{proposal.quote_spread_status ?? "—"}</div>
                  </td>
                  <td>
                    <StatusPill status={outcomeTone(proposal.risk_outcome)} />
                    {proposal.blockers.length > 0 && (
                      <div className="detail">{proposal.blockers.length} blocking</div>
                    )}
                  </td>
                  <td>
                    <PolicyBadge proposal={proposal} />
                  </td>
                  <td>
                    <StatusPill status={statusTone(proposal.status)} />
                    <div className="detail">{proposal.status}</div>
                    <div>
                      <ExpiryNote proposal={proposal} />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function ProposalDetail() {
  const { proposalId } = useParams();
  return <ProposalRecord key={proposalId} proposalId={proposalId ?? ""} />;
}

function ProposalRecord({ proposalId }: { proposalId: string }) {
  const { data, error, refresh } = usePolling(() => api.proposal(proposalId), 15_000);
  const risk = usePolling(() => api.proposalRisk(proposalId), 30_000);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<"approve" | "reject" | null>(null);

  const act = useCallback(
    async (run: () => Promise<Proposal>) => {
      setBusy(true);
      setActionError(null);
      try {
        await run();
      } catch (cause) {
        // A 409/410/422 is an *answer* from the risk engine or the state
        // machine, so it is shown verbatim rather than retried.
        setActionError(
          cause instanceof ApiError ? `${cause.status}: ${cause.message}` : String(cause),
        );
      } finally {
        setBusy(false);
        setConfirming(null);
        refresh();
        risk.refresh();
      }
    },
    [refresh, risk],
  );

  if (!data) {
    return (
      <section>
        <Link className="back-link" to="/proposals">
          ← Proposals
        </Link>
        <p role={error ? "alert" : undefined} className={error ? "error" : "muted"}>
          {error ?? "Loading proposal…"}
        </p>
      </section>
    );
  }

  return (
    <section>
      <Link className="back-link" to="/proposals">
        ← Proposals
      </Link>
      <div className="refresh-row">
        <div>
          <h1 className="page-title">
            {data.side} {data.proposed_quantity} {data.company_name ?? data.broker_ticker}
          </h1>
          <p className="page-subtitle">
            <StatusPill status={statusTone(data.status)} /> {data.status} ·{" "}
            <PolicyBadge proposal={data} /> · <ExpiryNote proposal={data} />
          </p>
        </div>
        <button onClick={refresh}>Refresh</button>
      </div>

      <div className="banner banner-safe">
        <div className="banner-title">No broker order has been sent</div>
        <div className="banner-body">{data.notice}</div>
      </div>

      {data.status_reason && (
        <div className="card">
          <h2>Status</h2>
          <p className="detail">{data.status_reason}</p>
          {data.invalidation_reason && (
            <p className="error">Invalidated: {data.invalidation_reason}</p>
          )}
        </div>
      )}

      <div className="grid">
        <div className="card">
          <h2>Instrument</h2>
          <dl className="kv">
            <dt>Company</dt>
            <dd>{data.company_name ?? "—"}</dd>
            <dt>Broker listing</dt>
            <dd className="mono">{data.broker_ticker}</dd>
            <dt>Market symbol</dt>
            <dd className="mono">{data.market_symbol ?? "—"}</dd>
            <dt>Exchange</dt>
            <dd>{data.exchange ?? "—"}</dd>
            <dt>ISIN</dt>
            <dd className="mono">{data.isin ?? "—"}</dd>
            <dt>Environment</dt>
            <dd>
              {data.broker} {data.broker_environment} · account {data.account_id}
            </dd>
          </dl>
        </div>

        <div className="card">
          <h2>Order parameters</h2>
          <dl className="kv">
            <dt>Side</dt>
            <dd>
              {data.side} ({data.order_type})
            </dd>
            <dt>Quantity</dt>
            <dd className="mono">
              {data.proposed_quantity}
              {data.max_quantity ? ` of ${data.max_quantity} permitted` : ""}
            </dd>
            <dt>Notional</dt>
            <dd className="mono">
              {data.estimated_notional} {data.account_currency}
              {data.max_notional ? ` of ${data.max_notional} permitted` : ""}
            </dd>
            <dt>Reference price</dt>
            <dd className="mono">
              {data.reference_price} {data.reference_currency}
            </dd>
            <dt>Currency</dt>
            <dd>
              instrument {data.instrument_currency ?? "?"} · account {data.account_currency}
            </dd>
          </dl>
        </div>

        <div className="card">
          <h2>Quote</h2>
          <dl className="kv">
            <dt>Source</dt>
            <dd className="mono">
              {data.price_source} · {data.quote_provider ?? "—"}/{data.quote_feed ?? "—"}
            </dd>
            <dt>Bid / ask</dt>
            <dd className="mono">
              {data.quote_bid ?? "—"} / {data.quote_ask ?? "—"}
            </dd>
            <dt>Mid</dt>
            <dd className="mono">{data.quote_mid ?? "—"}</dd>
            <dt>Spread</dt>
            <dd className="mono">
              {data.quote_spread ?? "—"} ({data.quote_spread_bps ?? "—"} bps ·{" "}
              {data.quote_spread_status ?? "—"})
            </dd>
            <dt>Age</dt>
            <dd className="mono">
              {data.quote_age_ms} ms · {formatTimestamp(data.quote_timestamp)}
            </dd>
            <dt>Session</dt>
            <dd>
              {data.market_session ?? "—"}{" "}
              <span className="faint">({data.market_session_source ?? "unknown source"})</span>
            </dd>
          </dl>
        </div>

        <div className="card">
          <h2>Research</h2>
          <dl className="kv">
            <dt>Action</dt>
            <dd>{data.research_action ?? "—"}</dd>
            <dt>Confidence</dt>
            <dd>
              {data.research_confidence === null
                ? "—"
                : `${Math.round(data.research_confidence * 100)}%`}
            </dd>
            <dt>Horizon</dt>
            <dd>{data.time_horizon ?? "—"}</dd>
          </dl>
          <p className="detail">
            Confidence is a model ranking, not a calibrated probability. It may only reduce a
            size inside the deterministic limits, and can never lift a block.
          </p>
          {data.thesis_summary && <p className="research-prose">{data.thesis_summary}</p>}
          {data.research_run_id && (
            <Link to={`/research/${data.research_run_id}`}>View research run</Link>
          )}
        </div>

        <div className="card">
          <h2>Authorization</h2>
          <dl className="kv">
            <dt>Policy</dt>
            <dd>
              <PolicyBadge proposal={data} />
            </dd>
            <dt>Source</dt>
            <dd>
              <Provenance proposal={data} />
            </dd>
            <dt>Rejected</dt>
            <dd>
              {data.rejected_at
                ? `${data.rejected_by} · ${formatTimestamp(data.rejected_at)}`
                : "—"}
            </dd>
            <dt>Created</dt>
            <dd>{formatTimestamp(data.created_at)}</dd>
            <dt>Expires</dt>
            <dd>{formatTimestamp(data.expires_at)}</dd>
            <dt>Broker order</dt>
            <dd>{data.broker_order_transmitted ? "SENT" : "not sent"}</dd>
          </dl>
        </div>
      </div>

      {(data.can_approve || data.can_reject || data.can_cancel) && (
        <div className="card">
          <h2>Actions</h2>
          {data.execution_policy === "AUTOMATIC" && !data.authorization_source && (
            <p className="detail">
              This proposal was generated under the AUTOMATIC policy but has not been
              system-authorized. It can still be authorized by a person.
            </p>
          )}
          {actionError && (
            <p role="alert" className="error">
              {actionError}
            </p>
          )}
          {confirming === "approve" ? (
            <p>
              Authorize {data.side} {data.proposed_quantity} {data.broker_ticker} at{" "}
              <span className="mono">{data.reference_price}</span>? The listing, account state,
              quote, spread and every risk rule are re-checked before this is recorded.{" "}
              <strong>
                Authorizing does not itself send an order; transmission is a separate,
                separately gated step, and every order is sent at most once.
              </strong>{" "}
              <button disabled={busy} onClick={() => act(() => api.approveProposal(data.id))}>
                Confirm authorization
              </button>{" "}
              <button disabled={busy} onClick={() => setConfirming(null)}>
                Cancel
              </button>
            </p>
          ) : confirming === "reject" ? (
            <p>
              Reject this proposal? Rejection is durable and terminal.{" "}
              <button disabled={busy} onClick={() => act(() => api.rejectProposal(data.id))}>
                Confirm rejection
              </button>{" "}
              <button disabled={busy} onClick={() => setConfirming(null)}>
                Cancel
              </button>
            </p>
          ) : (
            <p>
              {data.can_approve && (
                <button disabled={busy} onClick={() => setConfirming("approve")}>
                  Approve
                </button>
              )}{" "}
              {data.can_reject && (
                <button disabled={busy} onClick={() => setConfirming("reject")}>
                  Reject
                </button>
              )}{" "}
              {data.can_cancel && (
                <button disabled={busy} onClick={() => act(() => api.cancelProposal(data.id))}>
                  Cancel proposal
                </button>
              )}
            </p>
          )}
        </div>
      )}

      <ExecutionPanel proposalId={data.id} />

      {data.blockers.length > 0 && (
        <div className="card">
          <h2>Blockers</h2>
          <ul>
            {data.blockers.map((item) => (
              <li key={item} className="error">
                {item}
              </li>
            ))}
          </ul>
        </div>
      )}

      {(data.warnings.length > 0 || data.reductions.length > 0) && (
        <div className="card">
          <h2>Warnings and reductions</h2>
          <ul>
            {data.reductions.map((item) => (
              <li key={item}>{item}</li>
            ))}
            {data.warnings.map((item) => (
              <li key={item} className="muted">
                {item}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="card">
        <h2>Sizing</h2>
        <ul>
          {data.sizing_reasons.map((item) => (
            <li key={item} className="detail">
              {item}
            </li>
          ))}
        </ul>
      </div>

      <div className="card">
        <h2>
          Risk decision{" "}
          <StatusPill status={outcomeTone(data.risk_outcome)} />{" "}
          <span className="faint mono">policy {data.risk_policy_version ?? "—"}</span>
        </h2>
        <Rules rules={data.risk_rules} />
      </div>

      {risk.data && risk.data.evaluations.length > 0 && (
        <div className="card">
          <h2>Evaluation history</h2>
          {risk.data.evaluations.map((evaluation) => (
            <details key={evaluation.id}>
              <summary>
                {evaluation.stage} · {evaluation.outcome} · {formatTimestamp(evaluation.created_at)}{" "}
                {evaluation.actor ? `· ${evaluation.actor}` : ""}
              </summary>
              {evaluation.detail && <p className="error">{evaluation.detail}</p>}
              <Rules rules={evaluation.rules} />
            </details>
          ))}
        </div>
      )}
    </section>
  );
}
