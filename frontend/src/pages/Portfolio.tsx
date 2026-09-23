import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { PortfolioPosition, PortfolioResponse } from "../api/types";
import { Async, EmptyState, PageHeader, RefreshButton, TableWrap } from "../components/Page";
import {
  formatDecimal,
  formatDuration,
  formatQuantity,
  formatMoney,
  formatRelative,
  formatTimestamp,
  isNegative,
} from "../components/formats";
import { usePolling } from "../components/usePolling";

/**
 * The broker account, as StockBrain last mirrored it.
 *
 * Deliberately the stored snapshot rather than a live broker read. Trading
 * 212's account endpoint allows one request every five seconds; a page an
 * operator leaves open would spend that allowance on nothing, and the risk
 * engine — the only thing that must be certain about cash — refreshes on its
 * own schedule and independently refuses a snapshot older than its freshness
 * limit.
 *
 * So the honest presentation is the snapshot *with its age attached*, and a
 * loud note when it is older than the number a trade would have been sized on.
 * A cash balance displayed next to a live proposal without that note invites
 * exactly the wrong arithmetic.
 *
 * Nothing on this page is computed. Every figure is rendered as the server
 * persisted it: no client-side totals, no derived P/L, no currency conversion.
 */

const REFRESH_MS = 30_000;

export function Portfolio() {
  const portfolio = usePolling<PortfolioResponse>(api.portfolio, REFRESH_MS);

  return (
    <>
      <PageHeader
        title="Portfolio"
        subtitle="Cash and open positions from the most recent broker snapshot. Read from StockBrain's mirror, never by calling the broker — the account endpoint is rate-limited and the risk engine refreshes it on its own schedule."
        actions={<RefreshButton onClick={portfolio.refresh} busy={portfolio.loading} />}
      />

      <Async state={portfolio} errorTitle="Portfolio unavailable" rows={5}>
        {(data) =>
          !data.available ? (
            <EmptyState
              title="No broker snapshot yet"
              actions={
                <>
                  <Link className="button-quiet" to="/settings">
                    Broker settings
                  </Link>
                  <Link className="button-quiet" to="/health">
                    Broker health
                  </Link>
                </>
              }
            >
              <p>
                {data.reason ??
                  "Nothing has been captured from the broker yet."}{" "}
                Cash and positions are mirrored by a scheduled refresh once
                Trading 212 credentials are configured and reachable.
              </p>
            </EmptyState>
          ) : (
            <>
              <FreshnessBanner data={data} />
              <Summary data={data} />
              <Positions data={data} />
            </>
          )
        }
      </Async>
    </>
  );
}

function FreshnessBanner({ data }: { data: PortfolioResponse }) {
  const live = (data.broker_environment ?? "").toLowerCase() === "live";
  if (!data.stale) {
    return (
      <div className="banner banner-safe">
        <div className="banner-title">
          <span>
            {data.broker.toUpperCase()} · {(data.broker_environment ?? "unknown").toUpperCase()}
            {live ? " — REAL MONEY" : " (paper)"}
          </span>
          <span className="faint mono">
            captured {formatRelative(data.captured_at)} · account {data.account_id ?? "—"}
          </span>
        </div>
        <div className="banner-body">
          Fresh enough for the risk engine to size against
          {data.max_age_seconds !== null && (
            <> (limit {formatDuration(data.max_age_seconds)})</>
          )}
          .
        </div>
      </div>
    );
  }
  return (
    <div className="banner banner-warn">
      <div className="banner-title">
        <span>Snapshot is stale</span>
        <span className="faint mono">captured {formatTimestamp(data.captured_at)}</span>
      </div>
      <div className="banner-body">
        These figures are older than the{" "}
        {data.max_age_seconds !== null ? formatDuration(data.max_age_seconds) : "configured"}{" "}
        freshness the risk engine requires, so they are <strong>not</strong> the
        numbers a trade would be sized against right now. A proposal cannot be
        authorized on a snapshot this old — the engine refuses rather than
        assuming. Check the broker's health if this does not clear.
      </div>
    </div>
  );
}

function Summary({ data }: { data: PortfolioResponse }) {
  const negative = isNegative(data.result_value);
  return (
    <div className="grid grid-tight">
      <div className="card">
        <h2>Account value</h2>
        <div className="metric">{formatMoney(data.total_value, data.currency)}</div>
        <div className="metric-note">
          {data.position_count} position{data.position_count === 1 ? "" : "s"} · account{" "}
          {data.account_id ?? "—"}
        </div>
      </div>
      <div className="card">
        <h2>Invested</h2>
        <div className="metric">{formatMoney(data.invested_value, data.currency)}</div>
        <div className="metric-note">at the broker's own valuation</div>
      </div>
      <div className="card">
        <h2>Result</h2>
        <div className={`metric ${negative ? "metric-bad" : "metric-ok"}`}>
          {formatMoney(data.result_value, data.currency)}
        </div>
        <div className="metric-note">unrealised, as the broker reports it</div>
      </div>
      <div className="card">
        <h2>Cash available</h2>
        <div className="metric">{formatMoney(data.cash_available, data.currency)}</div>
        <div className="metric-note">
          {formatMoney(data.cash_reserved, data.currency)} reserved ·{" "}
          {formatMoney(data.cash_in_pies, data.currency)} in pies
        </div>
      </div>
    </div>
  );
}

/** A rule id in the words the operator reads, never the raw enum. */
function exitRuleLabel(rule: string | null): string {
  switch (rule) {
    case "hard_stop":
      return "stop";
    case "volatility_stop":
      return "volatility";
    case "trailing_stop":
      return "trail";
    case "roi_target":
      return "target";
    case "horizon_elapsed":
      return "horizon";
    case "thesis_superseded":
      return "thesis";
    default:
      return rule ?? "—";
  }
}

/**
 * The two exit columns: the nearest floor with its rule, and every floor.
 *
 * A position the risk layer could not describe (no floors at all) is not the
 * same as one StockBrain never opened: the first is a gap in what we know, the
 * second is a fact about the position, and both are said plainly rather than
 * rendered as zeroes.
 */
function ExitCells({ position }: { position: PortfolioPosition }) {
  const exit = position.exit;
  if (!exit) {
    return (
      <td className="tight detail" colSpan={2}>
        floors unavailable
      </td>
    );
  }
  if (!exit.managed) {
    return (
      <td className="tight detail" colSpan={2}>
        not managed{exit.reason ? ` — ${exit.reason}` : ""}
      </td>
    );
  }
  if (exit.managed && exit.hard_stop === null) {
    return (
      <td className="tight detail" colSpan={2}>
        floors unavailable{exit.reason ? ` — ${exit.reason}` : ""}
      </td>
    );
  }
  return (
    <>
      <td className="num">
        {formatDecimal(exit.nearest_floor, { places: 2 })}
        <span className="detail"> · {exitRuleLabel(exit.nearest_rule)}</span>
      </td>
      <td className="tight detail">
        <div>stop {formatDecimal(exit.hard_stop, { places: 2 })}</div>
        <div>vol {formatDecimal(exit.volatility_floor, { places: 2 })}</div>
        <div>trail {formatDecimal(exit.trailing_floor, { places: 2 })}</div>
        <div>target {formatDecimal(exit.roi_target_price, { places: 2 })}</div>
        <div>horizon {formatTimestamp(exit.horizon_ends_at)}</div>
      </td>
    </>
  );
}

function Positions({ data }: { data: PortfolioResponse }) {
  const [reviewing, setReviewing] = useState<string | null>(null);
  const [reviewNotice, setReviewNotice] = useState<string | null>(null);

  async function review(ticker?: string) {
    const label = ticker ?? "all active holdings";
    if (!window.confirm(
      `Re-review ${label}? A SELL/REDUCE conclusion will enter the configured trade authorization flow.`,
    )) return;
    setReviewing(ticker ?? "all");
    setReviewNotice(null);
    try {
      const result = await api.reviewPositions(ticker);
      const queued = Object.keys(result.requested);
      const skipped = Object.entries(result.skipped).map(([key, reason]) => `${key}: ${reason}`);
      setReviewNotice([
        queued.length ? `Queued ${queued.join(", ")}.` : "Nothing queued.",
        ...skipped,
      ].join(" "));
    } catch (error) {
      setReviewNotice(error instanceof Error ? error.message : "Review request failed.");
    } finally {
      setReviewing(null);
    }
  }

  if (data.positions.length === 0) {
    return (
      <EmptyState title="No open positions">
        <p>
          The snapshot captured at {formatTimestamp(data.captured_at)} holds cash
          and no holdings.
        </p>
      </EmptyState>
    );
  }
  return (
    <div className="card card-table">
      <div className="card-head">
        <h2>Open positions</h2>
        <div className="button-row">
          <span className="detail">
            {data.positions.length} shown
            {data.position_count > data.positions.length && ` of ${data.position_count}`}
          </span>
          <button disabled={reviewing !== null} onClick={() => void review()}>
            {reviewing === "all" ? "Queueing…" : "Re-review all"}
          </button>
        </div>
      </div>
      {reviewNotice && <div className="banner banner-info" role="status">{reviewNotice}</div>}
      <TableWrap>
        <table>
          <thead>
            <tr>
              <th>Listing</th>
              <th className="num">Quantity</th>
              <th className="num">Average</th>
              <th className="num">Current</th>
              <th className="num">Nearest exit</th>
              <th className="tight">Floors</th>
              <th className="num">Result</th>
              <th className="tight">Synced</th>
              <th className="tight">Review</th>
            </tr>
          </thead>
          <tbody>
            {data.positions.map((position) => (
              <tr key={position.broker_ticker}>
                <td>
                  <span className="event-title">{position.name ?? position.broker_ticker}</span>
                  {/* Only when it adds something: repeating the ticker under
                      itself for an instrument with no stored name was two
                      identical lines. */}
                  <div className="detail mono">
                    {position.name ? `${position.broker_ticker} · ` : ""}
                    {position.currency ?? "currency unknown"}
                  </div>
                </td>
                <td className="num">
                  {formatQuantity(position.quantity)}
                  {position.quantity_available !== null &&
                    position.quantity_available !== position.quantity && (
                      <div className="detail">
                        {formatQuantity(position.quantity_available)} available
                      </div>
                    )}
                </td>
                <td className="num">{formatDecimal(position.average_price, { places: 2 })}</td>
                <td className="num">{formatDecimal(position.current_price, { places: 2 })}</td>
                <ExitCells position={position} />
                <td className={`num ${isNegative(position.ppl) ? "metric-bad" : ""}`}>
                  {formatDecimal(position.ppl, { places: 2 })}
                </td>
                <td className="tight detail" title={formatTimestamp(position.last_synced_at)}>
                  {formatRelative(position.last_synced_at)}
                </td>
                <td className="tight">
                  <button
                    disabled={reviewing !== null || position.exit?.managed === false}
                    title={position.exit?.managed === false ? "No StockBrain opening thesis" : undefined}
                    onClick={() => void review(position.broker_ticker)}
                  >
                    {reviewing === position.broker_ticker ? "Queueing…" : "Re-review"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableWrap>
      <p className="detail" style={{ padding: "10px 16px 14px" }}>
        Quantities, prices and results are the broker's own figures, rendered
        exactly as they were stored. Nothing on this page is recomputed in the
        browser, and prices here are display data — Trading 212's API market data
        is not real-time and is never used to size a trade.
      </p>
    </div>
  );
}
