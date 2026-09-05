import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { InstrumentCandidate, Resolution } from "../api/types";
import { StatusPill } from "../components/StatusPill";
import { formatRelative, formatTimestamp } from "../components/formats";
import { usePolling } from "../components/usePolling";

/**
 * Instrument resolution and market-data provenance.
 *
 * The page exists so a *refusal* can be inspected. An AMBIGUOUS mapping nobody
 * can look at is indistinguishable from a bug, so every rejected alternative is
 * rendered next to the hint that produced it.
 */

const STATUS_ORDER = [
  "RESOLVED",
  "AMBIGUOUS",
  "NOT_FOUND",
  "UNSUPPORTED",
  "PENDING",
] as const;

/** Resolution status mapped onto the shared health palette. */
function statusTone(status: string) {
  if (status === "RESOLVED") return "HEALTHY" as const;
  if (status === "AMBIGUOUS") return "DEGRADED" as const;
  if (status === "PENDING") return "UNKNOWN" as const;
  return "DOWN" as const;
}

function capabilityTone(state: string) {
  if (state === "HEALTHY") return "HEALTHY" as const;
  if (state === "DISABLED") return "DISABLED" as const;
  if (state === "ENTITLEMENT_MISSING" || state === "DEGRADED") return "DEGRADED" as const;
  if (state === "UNKNOWN") return "UNKNOWN" as const;
  return "DOWN" as const;
}

function Alternatives({ items }: { items: InstrumentCandidate[] }) {
  if (items.length === 0) return null;
  return (
    <div className="detail" style={{ marginTop: 6 }}>
      <strong>Candidates:</strong>{" "}
      {items.map((candidate, index) => (
        <span key={candidate.broker_instrument_id}>
          {index > 0 && " · "}
          <span className="mono">{candidate.broker_ticker}</span>
          {candidate.exchange ? ` (${candidate.exchange}` : ""}
          {candidate.exchange && candidate.currency ? `, ${candidate.currency}` : ""}
          {candidate.exchange ? ")" : ""}
          {candidate.isin ? ` · ${candidate.isin}` : ""}
        </span>
      ))}
    </div>
  );
}

function ResolutionRow({ row }: { row: Resolution }) {
  return (
    <tr>
      <td>
        <div>{row.company_name_hint}</div>
        <div className="event-meta detail">
          model hint:{" "}
          <span className="mono">{row.model_ticker_hint ?? "—"}</span>
          {row.model_exchange_hint ? ` @ ${row.model_exchange_hint}` : ""}
          {row.event_id && (
            <>
              {" · "}
              <Link to={`/events/${row.event_id}`}>event</Link>
            </>
          )}
        </div>
        {row.notes && <div className="detail">{row.notes}</div>}
        <Alternatives items={row.alternatives} />
      </td>
      <td>
        <StatusPill status={statusTone(row.status)} />
        <div className="detail">{row.status}</div>
      </td>
      <td className="mono">{row.broker_ticker ?? "—"}</td>
      <td className="mono">{row.market_symbol ?? "—"}</td>
      <td>{row.exchange ?? "—"}</td>
      <td className="mono">{row.currency ?? "—"}</td>
      <td className="mono">{row.isin ?? "—"}</td>
      <td className="mono">
        {row.confidence === null ? "—" : row.confidence.toFixed(2)}
        <div className="detail">{row.method ?? "—"}</div>
      </td>
    </tr>
  );
}

export function Instruments() {
  const [statusFilter, setStatusFilter] = useState<string>("");
  const resolutions = usePolling(
    () =>
      api.resolutions(
        (statusFilter || undefined) as Parameters<typeof api.resolutions>[0],
      ),
    30_000,
  );
  const sync = usePolling(api.instrumentSyncStatus, 60_000);
  const marketData = usePolling(api.marketDataHealth, 60_000);
  const aliases = usePolling(api.aliases, 300_000);

  const counts = resolutions.data?.counts_by_status ?? {};

  return (
    <>
      <div className="refresh-row">
        <div>
          <h1 className="page-title">Instruments</h1>
          <p className="page-subtitle" style={{ marginBottom: 0 }}>
            How each classifier company hint mapped onto a verified broker
            instrument. A hint is a search key, never an identity: only a
            Trading&nbsp;212 instrument may ever reach an order request, and an
            ambiguous listing blocks rather than resolving.
          </p>
        </div>
        <button onClick={resolutions.refresh} disabled={resolutions.loading}>
          {resolutions.loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {resolutions.error && <div className="error">{resolutions.error}</div>}

      <div className="grid">
        <div className="card">
          <h2>Broker universe</h2>
          <div className="metric">{sync.data?.instruments_active ?? "—"}</div>
          <div className="metric-note">
            {sync.data
              ? `${sync.data.instruments_total} known · ${sync.data.with_isin} with ISIN · ` +
                `${sync.data.with_exchange} with a derived exchange`
              : "loading"}
          </div>
          <div className="detail">
            {sync.data?.configured
              ? `Last refreshed ${formatRelative(sync.data.last_refreshed_at)}`
              : "Trading 212 credentials are not configured"}
          </div>
        </div>

        <div className="card">
          <h2>Exchanges</h2>
          <div className="metric">{sync.data?.exchanges ?? "—"}</div>
          <div className="metric-note">
            {sync.data
              ? `${sync.data.working_schedules} working schedules`
              : "loading"}
          </div>
          <div className="detail">
            Trading 212 supplies no exchange on an instrument; it is derived
            from the working schedule.
          </div>
        </div>

        <div className="card">
          <h2>Market data</h2>
          <div className="metric">
            <StatusPill status={capabilityTone(marketData.data?.state ?? "UNKNOWN")} />
          </div>
          <div className="metric-note">
            {marketData.data
              ? `${marketData.data.state}${marketData.data.feed ? ` · feed ${marketData.data.feed}` : ""}`
              : "loading"}
          </div>
          <div className="detail">
            {marketData.data?.realtime_pricing_usable
              ? `Real-time pricing usable · probe age ${marketData.data.probe_quote_age_ms ?? "—"}ms · ` +
                `max quote age ${marketData.data.max_quote_age_seconds}s`
              : (marketData.data?.blockers.join("; ") ??
                "Pricing unavailable — proposal sizing stays blocked")}
          </div>
        </div>

        <div className="card">
          <h2>Curated aliases</h2>
          <div className="metric">{aliases.data?.length ?? "—"}</div>
          <div className="metric-note">
            {aliases.data
              ? `${aliases.data.filter((a) => a.is_authoritative).length} authoritative`
              : "loading"}
          </div>
          <div className="detail">
            Manual mappings for names evidence alone cannot settle.
          </div>
        </div>
      </div>

      <div className="filters">
        <label>
          Status
          <select
            value={statusFilter}
            onChange={(event) => {
              setStatusFilter(event.target.value);
              resolutions.refresh();
            }}
          >
            <option value="">All</option>
            {STATUS_ORDER.map((status) => (
              <option key={status} value={status}>
                {status} ({counts[status] ?? 0})
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Company hint</th>
              <th>Status</th>
              <th>T212 instrument</th>
              <th>Market symbol</th>
              <th>Exchange</th>
              <th>Currency</th>
              <th>ISIN</th>
              <th>Confidence</th>
            </tr>
          </thead>
          <tbody>
            {resolutions.data?.items.map((row) => (
              <ResolutionRow key={row.impact_id} row={row} />
            ))}
            {resolutions.data && resolutions.data.items.length === 0 && (
              <tr>
                <td colSpan={8} className="muted">
                  No company impacts have been resolved yet.
                </td>
              </tr>
            )}
            {!resolutions.data && (
              <tr>
                <td colSpan={8} className="muted">
                  Loading…
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {aliases.data && aliases.data.length > 0 && (
        <div className="card">
          <h2>Alias mappings</h2>
          <table>
            <thead>
              <tr>
                <th>Alias</th>
                <th>Type</th>
                <th>Company</th>
                <th>Scope</th>
                <th>Authoritative</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {aliases.data.map((alias) => (
                <tr key={alias.id}>
                  <td>{alias.alias}</td>
                  <td className="mono">{alias.alias_type}</td>
                  <td>{alias.company_name ?? "—"}</td>
                  <td className="detail">
                    {[alias.exchange, alias.currency, alias.isin]
                      .filter(Boolean)
                      .join(" · ") || "any listing"}
                  </td>
                  <td className="mono">{alias.is_authoritative ? "yes" : "no"}</td>
                  <td className="detail">{alias.notes ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {sync.data?.last_refreshed_at && (
        <p className="detail">
          Instrument metadata last refreshed{" "}
          {formatTimestamp(sync.data.last_refreshed_at)}.
        </p>
      )}
    </>
  );
}
