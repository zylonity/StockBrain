import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../api/client";
import type { ExecutionAttempt, ProposalExecution } from "../api/types";
import { LoadingRows, TableWrap } from "./Page";
import { StatusPill } from "./StatusPill";
import { formatRelative, formatTimestamp } from "./formats";

/**
 * Whether an environment should look dangerous.
 *
 * Demo and live share an order id space and a request shape, and the only thing
 * separating a paper trade from a real one is this string — so it is rendered as
 * a badge rather than as body text.
 */
function EnvironmentBadge({ environment }: { environment: string | null }) {
  const live = (environment ?? "").toLowerCase() === "live";
  return (
    <span className={`pill ${live ? "pill-down" : "pill-disabled"}`}>
      {live ? "LIVE — REAL MONEY" : `${(environment ?? "unknown").toUpperCase()} (paper)`}
    </span>
  );
}

/**
 * The warning that matters more than any other in this application.
 *
 * When an order's state is unknown, the obvious reaction is to place it again —
 * and that is the one action that turns an unknown into a real, duplicated
 * position. Trading 212 documents the order endpoint as non-idempotent, so
 * there is no resend button anywhere and this explains why.
 */
function AmbiguousWarning({ execution }: { execution: ProposalExecution }) {
  if (!execution.ambiguous) return null;
  return (
    <div className="banner banner-live">
      <div className="banner-title">
        <span>⚠ ORDER STATE UNKNOWN — DO NOT RESEND</span>
        <StatusPill status="DOWN" />
      </div>
      <div className="banner-body">{execution.notice}</div>
      <ul>
        <li>StockBrain will not retry. Reconciliation is reading Trading 212.</li>
        <li>
          Do not place this trade manually until reconciliation reports the outcome — the
          order may already exist.
        </li>
        <li>This proposal continues to reserve its exposure until the outcome is known.</li>
      </ul>
    </div>
  );
}

function AttemptRow({
  attempt,
  onReconcile,
  busy,
}: {
  attempt: ExecutionAttempt;
  onReconcile: (id: string) => void;
  busy: boolean;
}) {
  return (
    <tr>
      <td className="mono">{attempt.attempt_number}</td>
      <td>
        <span className={`pill ${attempt.ambiguous ? "pill-down" : "pill-disabled"}`}>
          {attempt.outcome}
        </span>
      </td>
      <td className="mono">{attempt.sent_to_broker ? "yes" : "no"}</td>
      <td className="mono detail" title={formatTimestamp(attempt.sent_at)}>
        {formatRelative(attempt.sent_at)}
      </td>
      <td className="mono">{attempt.broker_order_id ?? "—"}</td>
      <td className="mono">{attempt.http_status ?? "—"}</td>
      <td className="detail">
        {attempt.error_category ?? "—"}
        {attempt.error && <div className="faint">{attempt.error}</div>}
      </td>
      <td className="detail">
        {attempt.reconciliation_result ?? "—"}
        {attempt.reconciliation_attempts > 0 && (
          <span className="faint mono"> ({attempt.reconciliation_attempts} pass)</span>
        )}
      </td>
      <td>
        {/* A read, and therefore safe to repeat. There is no resend counterpart. */}
        {attempt.sent_to_broker && (
          <button disabled={busy} onClick={() => onReconcile(attempt.id)}>
            Reconcile
          </button>
        )}
      </td>
    </tr>
  );
}

/**
 * One proposal's journey to the broker: attempts, orders, and what is known.
 *
 * Deliberately offers exactly one action — reconcile — because that is the only
 * safe one. Everything else here is a read.
 */
export function ExecutionPanel({ proposalId }: { proposalId: string }) {
  const [execution, setExecution] = useState<ProposalExecution | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async (): Promise<void> => {
    setError(null);
    try {
      setExecution(await api.proposalExecution(proposalId));
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setLoaded(true);
    }
  }, [proposalId]);

  const reconcile = useCallback(
    async (attemptId: string): Promise<void> => {
      setBusy(true);
      setError(null);
      try {
        await api.reconcileAttempt(attemptId);
      } catch (cause) {
        setError(cause instanceof ApiError ? cause.message : String(cause));
      } finally {
        setBusy(false);
        await load();
      }
    },
    [load],
  );

  // In an effect, not during render. The previous version called `load()` from
  // the render body when `loaded` was false, which sets state during render --
  // React 19 warns, and under Strict Mode it fired the request twice on mount.
  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="card">
      <div className="card-head">
        <h2>Execution</h2>
        <button onClick={() => void load()} disabled={busy}>
          Refresh
        </button>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {execution ? (
        <>
          <AmbiguousWarning execution={execution} />
          <p className="detail">
            <EnvironmentBadge environment={execution.broker_environment} />{" "}
            <span className="mono">
              authorized by {execution.authorization_source ?? "nobody"} · policy{" "}
              {execution.execution_policy}
            </span>
          </p>
          {!execution.ambiguous && <p className="detail">{execution.notice}</p>}

          {execution.attempts.length === 0 ? (
            <p className="muted">No transmission has been attempted.</p>
          ) : (
            <TableWrap>
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Outcome</th>
                  <th>Sent</th>
                  <th>Sent at</th>
                  <th>Broker order</th>
                  <th>HTTP</th>
                  <th>Error</th>
                  <th>Reconciliation</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {execution.attempts.map((attempt) => (
                  <AttemptRow
                    key={attempt.id}
                    attempt={attempt}
                    onReconcile={(id) => void reconcile(id)}
                    busy={busy}
                  />
                ))}
              </tbody>
            </table>
            </TableWrap>
          )}

          {execution.orders.length > 0 && (
            <>
              <h3>Broker orders</h3>
              <TableWrap>
              <table>
                <thead>
                  <tr>
                    <th>Order</th>
                    <th>Status</th>
                    <th>Filled</th>
                    <th>Initiated from</th>
                    <th>Discovered by</th>
                    <th>Synced</th>
                  </tr>
                </thead>
                <tbody>
                  {execution.orders.map((order) => (
                    <tr key={order.broker_order_id}>
                      <td className="mono">{order.broker_order_id}</td>
                      <td className="mono">{order.broker_status ?? "—"}</td>
                      <td className="mono">
                        {order.filled_quantity ?? "0"} / {order.quantity}
                      </td>
                      <td className="mono">{order.initiated_from ?? "—"}</td>
                      <td className="mono detail">
                        {order.discovered_by_reconciliation ? "reconciliation" : "submission"}
                      </td>
                      <td
                        className="mono detail"
                        title={formatTimestamp(order.last_synced_at)}
                      >
                        {formatRelative(order.last_synced_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </TableWrap>
            </>
          )}
        </>
      ) : loaded ? (
        <p className="muted">
          No execution record for this proposal — nothing has been transmitted.
        </p>
      ) : (
        <LoadingRows rows={2} label="Loading execution history" />
      )}
    </div>
  );
}
