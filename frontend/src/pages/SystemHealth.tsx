import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../api/client";
import type {
  DiscoveryStatus,
  HealthResponse,
  ProviderHealth,
  ProviderStatus,
  ProvidersResponse,
} from "../api/types";
import { ControlBanner } from "../components/ControlBanner";
import { Async, EmptyState, PageHeader, RefreshButton, TableWrap } from "../components/Page";
import { StatusLegend, StatusPill } from "../components/StatusPill";
import { STATUS_ACTION } from "../components/statuses";
import { formatRelative, formatTimestamp, humanise } from "../components/formats";
import { usePolling } from "../components/usePolling";

/**
 * The operational page: what is working, what is not, and what to do about it.
 *
 * Three changes of emphasis from a plain status board:
 *
 * **A status is never only a label.** Every row carries what the state means
 * and what the next action is, because DISABLED and DOWN look equally alarming
 * and only one of them is a problem, and because BUDGET_EXHAUSTED reads as a
 * fault when it is a spending limit working correctly.
 *
 * **Every provider links to its own evidence.** "View logs" filters the shared
 * Logs page to that service, so triage does not begin with retyping a filter.
 *
 * **Providers are grouped by subsystem.** A dead news feed and a dead broker
 * are not comparable, and an ungrouped alphabetical list makes them look it.
 */

const REFRESH_MS = 15_000;

/** Which subsystem each provider belongs to, mirroring the server's own map. */
const SUBSYSTEM_OF: Record<string, string> = {
  postgres: "database",
  alpaca_news: "discovery",
  brave: "discovery",
  exa: "discovery",
  firecrawl: "discovery",
  sec: "discovery",
  content_extraction: "discovery",
  llm: "research",
  tradingagents: "research",
  alpaca_market_data: "research",
  fred: "research",
  trading212: "execution",
  telegram: "notifications",
};

const SUBSYSTEM_TITLES: Record<string, string> = {
  database: "Database",
  discovery: "Discovery",
  research: "Research and market data",
  execution: "Execution",
  notifications: "Notifications",
  other: "Other",
};

const SUBSYSTEM_NOTES: Record<string, string> = {
  database:
    "The only load-bearing dependency. Losing it makes the application not-ready; everything else can only degrade a subsystem.",
  discovery:
    "Where events come from. Redundant by design: one dead source degrades discovery rather than stopping it.",
  research:
    "What turns a candidate into a thesis, and what prices it. A missing execution-grade quote blocks sizing rather than guessing.",
  execution:
    "The broker. Its health decides whether an authorized proposal can be transmitted or reconciled.",
  notifications:
    "How the system reaches you when nobody is watching. A failure here is silent by definition, which is why it is a provider.",
  other: "Providers not attached to a named subsystem.",
};

export function SystemHealth() {
  const providers = usePolling<ProvidersResponse>(api.providers, REFRESH_MS);
  const health = usePolling<HealthResponse>(api.health, REFRESH_MS);
  const control = usePolling(api.controlState, REFRESH_MS);
  const telegram = usePolling(api.telegramStatus, 30_000);
  const execution = usePolling(api.executionSummary, 20_000);
  const discovery = usePolling<DiscoveryStatus>(api.discoveryStatus, 30_000);
  // Both make one provider probe, so they poll slowly. FX in particular reaches
  // out to a rate source; asking it every fifteen seconds would be impolite to
  // a free public API for no operational benefit.
  const fx = usePolling(api.fxStatus, 120_000);
  const security = usePolling(api.webSecurity, 300_000);

  const [busy, setBusy] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);

  /**
   * Every control change goes to the server and the answer is re-read from it.
   * Nothing here decides anything locally: the durable row is the state, and
   * the button is only a request to change it.
   */
  async function change(action: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    setControlError(null);
    try {
      await action();
    } catch (cause) {
      setControlError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setBusy(false);
      control.refresh();
    }
  }

  const halted = control.data?.trading_halted ?? false;
  const killed = control.data?.kill_switch.active ?? false;
  const paused = control.data?.paused.active ?? false;

  const grouped = useMemo(() => groupProviders(providers.data?.providers ?? []), [providers.data]);
  const attention = useMemo(
    () =>
      (providers.data?.providers ?? []).filter((provider) =>
        ["DOWN", "DEGRADED", "BUDGET_EXHAUSTED"].includes(provider.status),
      ),
    [providers.data],
  );

  return (
    <>
      <PageHeader
        title="System health"
        subtitle="Every external dependency, what state it is in, and what that state means. A DOWN provider degrades only its own subsystem; DISABLED means not configured or deliberately switched off, and is never a fault."
        actions={
          <>
            <Link className="button-quiet" to="/logs">
              All logs
            </Link>
            <RefreshButton onClick={providers.refresh} busy={providers.loading} />
          </>
        }
      />

      {control.data && <ControlBanner control={control.data} />}

      <div className="grid grid-tight">
        <div className="card">
          <h2>Overall</h2>
          <div className="metric metric-sm">
            {health.data ? <StatusPill status={health.data.status} /> : <span className="faint">—</span>}
          </div>
          <div className="metric-note">
            {health.data
              ? `${health.data.app} ${health.data.version} · ${health.data.environment}`
              : "reading"}
          </div>
        </div>
        <div className="card">
          <h2>Needing attention</h2>
          <div className={`metric ${attention.length ? "metric-warn" : "metric-ok"}`}>
            {providers.data ? attention.length : "—"}
          </div>
          <div className="metric-note">
            {attention.length === 0
              ? "every configured provider is healthy or deliberately off"
              : attention.map((provider) => provider.provider).join(", ")}
          </div>
        </div>
        <div className="card">
          <h2>Trading control</h2>
          <div className="metric metric-sm">
            <StatusPill
              status={killed ? "DOWN" : paused ? "DEGRADED" : "HEALTHY"}
              label={killed ? "KILL SWITCH" : paused ? "PAUSED" : "RUNNING"}
            />
          </div>
          <div className="metric-note">
            {halted
              ? `since ${formatRelative(
                  (killed ? control.data?.kill_switch.changed_at : control.data?.paused.changed_at) ??
                    null,
                )}`
              : "proposals and authorization are permitted"}
          </div>
        </div>
        <div className="card">
          <h2>Discovery</h2>
          <div className="metric metric-sm">
            <StatusPill
              status={
                !discovery.data?.subsystem_running
                  ? "DOWN"
                  : discovery.data.paused || !discovery.data.discovery_enabled
                    ? "DISABLED"
                    : "HEALTHY"
              }
              label={
                !discovery.data
                  ? "UNKNOWN"
                  : !discovery.data.subsystem_running
                    ? "NOT RUNNING"
                    : discovery.data.paused
                      ? "HELD"
                      : discovery.data.discovery_enabled
                        ? "RUNNING"
                        : "OFF"
              }
            />
          </div>
          <div className="metric-note">
            {discovery.data
              ? `${discovery.data.jobs_pending} job${
                  discovery.data.jobs_pending === 1 ? "" : "s"
                } pending · last source ${formatRelative(discovery.data.stats.latest_source_at)}`
              : "reading"}
          </div>
        </div>
      </div>

      {/* ---------------------------------------------------------------- */}

      <div className="card">
        <div className="card-head">
          <h2>Execution control</h2>
          <Link className="button-quiet" to="/settings">
            Settings →
          </Link>
        </div>
        <p className="setting-description">
          Pausing stops new proposals and every authorization path — web,
          Telegram and automatic. The kill switch does the same and additionally
          denies any future order transmission. Neither closes a position and
          neither cancels a broker order: StockBrain has no order, cancel or
          amend path. Both are stored in PostgreSQL, so they survive a restart.
        </p>
        <div className="spread" style={{ marginTop: 10 }}>
          {/* The same pill the summary card above uses. Two spellings of one
              fact on one page is how an operator learns not to trust either. */}
          <StatusPill
            status={killed ? "DOWN" : paused ? "DEGRADED" : "HEALTHY"}
            label={killed ? "KILL SWITCH ENGAGED" : paused ? "PAUSED" : "RUNNING"}
          />
          <span className="button-row">
            <button
              onClick={() => void change(() => api.pauseTrading("paused from the web GUI"))}
              disabled={busy || paused}
            >
              Pause
            </button>
            <button
              onClick={() => void change(() => api.resumeTrading("resumed from the web GUI"))}
              disabled={busy || !paused}
            >
              Resume
            </button>
            <button
              className="button-danger"
              onClick={() =>
                void change(() => api.setKillSwitch(true, "emergency stop from the web GUI"))
              }
              disabled={busy || killed}
            >
              Engage kill switch
            </button>
            <button
              onClick={() =>
                void change(() => api.setKillSwitch(false, "released from the web GUI"))
              }
              disabled={busy || !killed}
            >
              Release kill switch
            </button>
          </span>
        </div>
        {controlError && (
          <p className="error" role="alert">
            {controlError}
          </p>
        )}
      </div>

      {/* ---------------------------------------------------------------- */}

      <h2 className="section-title">Providers</h2>
      <Async state={providers} errorTitle="Provider health unavailable" rows={6}>
        {(data) =>
          data.providers.length === 0 ? (
            <EmptyState title="No providers registered">
              <p>The health registry is empty, which should not happen. Check the logs.</p>
            </EmptyState>
          ) : (
            <>
              {Object.entries(grouped).map(([subsystem, rows]) => (
                <ProviderGroup key={subsystem} subsystem={subsystem} providers={rows} />
              ))}
              <p className="detail">Checked at {formatTimestamp(data.checked_at)}.</p>
            </>
          )
        }
      </Async>

      <div className="card">
        <h2>What each state means</h2>
        <StatusLegend />
      </div>

      {/* ---------------------------------------------------------------- */}

      <h2 className="section-title">Subsystems</h2>

      <div className="card">
        <div className="card-head">
          <h2>Broker execution</h2>
          <ServiceLinks service="trading212" settings="execution" />
        </div>
        {execution.data ? (
          <TableWrap>
            <table>
              <tbody>
                <tr>
                  <td>Environment</td>
                  <td>
                    <span
                      className={`pill ${
                        execution.data.broker_environment === "live" ? "pill-down" : "pill-disabled"
                      }`}
                    >
                      {execution.data.broker_environment === "live"
                        ? "LIVE — REAL MONEY"
                        : `${execution.data.broker_environment.toUpperCase()} (paper)`}
                    </span>
                  </td>
                </tr>
                <tr>
                  <td>Transmission permitted</td>
                  <td className="mono">
                    {execution.data.order_transmission_permitted ? "yes" : "no"}
                  </td>
                </tr>
                {execution.data.blockers.length > 0 && (
                  <tr>
                    <td>Blockers</td>
                    <td className="detail">
                      <ul className="reason-list">
                        {execution.data.blockers.map((blocker) => (
                          <li key={blocker}>{blocker}</li>
                        ))}
                      </ul>
                    </td>
                  </tr>
                )}
                <tr>
                  <td>Order endpoint</td>
                  <td className="mono detail">
                    {execution.data.order_endpoint} · idempotent:{" "}
                    {execution.data.order_endpoint_idempotent ? "yes" : "NO"}
                  </td>
                </tr>
                <tr>
                  <td>Attempts</td>
                  <td className="mono">
                    {Object.entries(execution.data.attempts_by_outcome)
                      .map(([outcome, count]) => `${outcome} ${count}`)
                      .join(" · ") || "none"}
                  </td>
                </tr>
                <tr>
                  <td>Awaiting reconciliation</td>
                  <td className="mono">
                    {execution.data.reconciliation_pending}
                    {execution.data.ambiguous_attempts > 0 && (
                      <span className="metric-bad">
                        {" "}
                        — {execution.data.ambiguous_attempts} ambiguous; DO NOT RESEND
                      </span>
                    )}
                  </td>
                </tr>
              </tbody>
            </table>
          </TableWrap>
        ) : execution.error ? (
          <p className="error">{execution.error}</p>
        ) : (
          <p className="muted">Reading…</p>
        )}
        {execution.data?.ambiguous_attempts ? (
          <p>
            <Link to="/proposals">Review the affected proposals →</Link>
          </p>
        ) : null}
        <p className="detail">{execution.data?.notice}</p>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Telegram</h2>
          <ServiceLinks service="telegram" settings="telegram" />
        </div>
        {telegram.data ? (
          <TableWrap>
            <table>
              <tbody>
                <tr>
                  <td>Status</td>
                  <td>
                    <StatusPill status={telegram.data.status} />
                    <div className="detail">{STATUS_ACTION[telegram.data.status]}</div>
                  </td>
                </tr>
                <tr>
                  <td>Transport</td>
                  <td className="mono">
                    {telegram.data.transport} · webhook{" "}
                    {telegram.data.webhook_configured ? "configured" : "none"}
                  </td>
                </tr>
                <tr>
                  <td>Bot</td>
                  <td className="mono">
                    {telegram.data.bot_configured ? "configured" : "not configured"} ·{" "}
                    {telegram.data.bot_identified ? "authenticated" : "not authenticated"}
                  </td>
                </tr>
                <tr>
                  <td>Last contact</td>
                  <td className="mono detail" title={formatTimestamp(telegram.data.last_contact_at)}>
                    {formatRelative(telegram.data.last_contact_at)}
                  </td>
                </tr>
                <tr>
                  <td>Last error</td>
                  <td className="mono detail">
                    {telegram.data.last_error_category ?? "none"} (
                    {telegram.data.consecutive_failures} consecutive)
                  </td>
                </tr>
                <tr>
                  <td>Authorized</td>
                  <td className="mono">
                    {telegram.data.authorized_users} user(s) ·{" "}
                    {telegram.data.authorized_chats} chat(s) ·{" "}
                    {telegram.data.notification_targets} notification target(s)
                  </td>
                </tr>
                {telegram.data.blockers.length > 0 && (
                  <tr>
                    <td>Blockers</td>
                    <td className="detail">
                      <ul className="reason-list">
                        {telegram.data.blockers.map((blocker) => (
                          <li key={blocker}>{blocker}</li>
                        ))}
                      </ul>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </TableWrap>
        ) : telegram.error ? (
          <p className="error">{telegram.error}</p>
        ) : (
          <p className="muted">Reading…</p>
        )}
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Foreign exchange</h2>
          <ServiceLinks service="fx" settings="market_data" />
        </div>
        {fx.data ? (
          <>
            <p className="setting-description">
              Cross-currency sizing depends on this entirely. A GBP account
              holding USD listings cannot size anything without a rate, and a
              rate that is missing, stale, of the wrong pair or of a grade this
              deployment has not permitted all block — never an assumed 1.0.
            </p>
            <TableWrap>
              <table>
                <tbody>
                  <tr>
                    <td>Usable now</td>
                    <td>
                      <StatusPill
                        status={
                          fx.data.provider === "none"
                            ? "DISABLED"
                            : fx.data.available
                              ? "HEALTHY"
                              : "DOWN"
                        }
                      />
                    </td>
                  </tr>
                  <tr>
                    <td>Provider</td>
                    <td className="mono">
                      {fx.data.provider}
                      {fx.data.grade && <span className="detail"> {fx.data.grade}</span>}
                    </td>
                  </tr>
                  <tr>
                    <td>{fx.data.probe_pair}</td>
                    <td className="mono">
                      {fx.data.probe_rate ?? "—"}
                      {fx.data.probe_age_seconds && (
                        <span className="detail">
                          {" "}
                          {Math.round(Number(fx.data.probe_age_seconds))}s old
                        </span>
                      )}
                    </td>
                  </tr>
                  <tr>
                    <td>Freshness limits</td>
                    <td className="mono detail">
                      {fx.data.max_age_seconds}s execution ·{" "}
                      {fx.data.reference_max_age_seconds}s reference
                      {fx.data.allow_reference_grade
                        ? " (reference permitted)"
                        : " (reference refused)"}
                    </td>
                  </tr>
                  <tr>
                    <td>Drift envelope</td>
                    <td className="mono detail">
                      {fx.data.max_rate_drift_pct} — past this an authorized proposal is
                      invalidated, never resized
                    </td>
                  </tr>
                  {fx.data.blockers.length > 0 && (
                    <tr>
                      <td>Blockers</td>
                      <td className="detail">
                        <ul className="reason-list">
                          {fx.data.blockers.map((blocker) => (
                            <li key={blocker}>{blocker}</li>
                          ))}
                        </ul>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </TableWrap>
          </>
        ) : fx.error ? (
          <p className="error">{fx.error}</p>
        ) : (
          <p className="muted">Probing the rate source…</p>
        )}
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Web access</h2>
          <ServiceLinks service="api" settings="web" />
        </div>
        {security.data ? (
          <>
            {!security.data.auth_effective && (
              <div className="banner banner-live">
                <div className="banner-title">This interface is not password-protected</div>
                <div className="banner-body">
                  It can authorize real broker orders.
                  {security.data.trusted_network_acknowledged
                    ? " A trusted-network model has been acknowledged; make sure the reverse proxy in front of it actually authenticates."
                    : " Set WEB_OWNER_PASSWORD_HASH — see docs/operations.md."}
                </div>
              </div>
            )}
            <TableWrap>
              <table>
                <tbody>
                  <tr>
                    <td>Authentication</td>
                    <td>
                      <StatusPill
                        status={
                          security.data.auth_effective
                            ? "HEALTHY"
                            : security.data.trusted_network_acknowledged
                              ? "DISABLED"
                              : "DOWN"
                        }
                      />
                    </td>
                  </tr>
                  <tr>
                    <td>Session</td>
                    <td className="mono detail">
                      {Math.round(security.data.session_ttl_seconds / 3600)}h · SameSite=
                      {security.data.cookie_samesite} ·{" "}
                      {security.data.cookie_secure ? "Secure" : "not Secure"}
                    </td>
                  </tr>
                  <tr>
                    <td>CSRF header</td>
                    <td className="mono detail">{security.data.csrf_header}</td>
                  </tr>
                  <tr>
                    <td>Unauthenticated paths</td>
                    <td className="mono detail">{security.data.public_paths.join(", ")}</td>
                  </tr>
                  {security.data.blockers.length > 0 && (
                    <tr>
                      <td>Blockers</td>
                      <td className="detail">
                        <ul className="reason-list">
                          {security.data.blockers.map((blocker) => (
                            <li key={blocker}>{blocker}</li>
                          ))}
                        </ul>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </TableWrap>
          </>
        ) : security.error ? (
          <p className="error">{security.error}</p>
        ) : (
          <p className="muted">Reading…</p>
        )}
      </div>
    </>
  );
}

/* ------------------------------------------------------------------------ */

function groupProviders(providers: ProviderHealth[]): Record<string, ProviderHealth[]> {
  const grouped: Record<string, ProviderHealth[]> = {};
  const order = ["database", "discovery", "research", "execution", "notifications", "other"];
  for (const provider of providers) {
    const subsystem = SUBSYSTEM_OF[provider.provider] ?? "other";
    (grouped[subsystem] ??= []).push(provider);
  }
  const sorted: Record<string, ProviderHealth[]> = {};
  for (const key of order) {
    const rows = grouped[key];
    if (rows) sorted[key] = rows.sort((a, b) => a.provider.localeCompare(b.provider));
  }
  return sorted;
}

function ProviderGroup({
  subsystem,
  providers,
}: {
  subsystem: string;
  providers: ProviderHealth[];
}) {
  return (
    <div className="card card-table">
      <div className="card-head">
        <h2>{SUBSYSTEM_TITLES[subsystem] ?? humanise(subsystem)}</h2>
      </div>
      <p className="detail">{SUBSYSTEM_NOTES[subsystem]}</p>
      <TableWrap>
        <table>
          <thead>
            <tr>
              <th>Provider</th>
              <th className="tight">State</th>
              <th>Detail and next step</th>
              <th className="tight">Last OK</th>
              <th className="tight">Checked</th>
              <th className="num">Fails</th>
              <th className="tight" />
            </tr>
          </thead>
          <tbody>
            {providers.map((provider) => (
              <ProviderRow key={provider.provider} provider={provider} />
            ))}
          </tbody>
        </table>
      </TableWrap>
    </div>
  );
}

function ProviderRow({ provider }: { provider: ProviderHealth }) {
  const status = provider.status as ProviderStatus;
  const metrics = Object.entries(provider.metrics ?? {}).filter(
    ([, value]) => value !== null && value !== undefined && value !== "",
  );
  return (
    <tr>
      <td className="mono nowrap">{provider.provider}</td>
      <td className="tight">
        <StatusPill status={status} />
      </td>
      <td className="detail">
        {provider.detail ?? <span className="faint">no detail reported</span>}
        <div className="faint">{STATUS_ACTION[status]}</div>
        {metrics.length > 0 && (
          <div className="log-fields">
            {metrics.map(([key, value]) => (
              <span key={key}>
                <span className="log-field-key">{key}=</span>
                {String(value)}
              </span>
            ))}
          </div>
        )}
      </td>
      <td className="tight detail" title={formatTimestamp(provider.last_ok_at)}>
        {formatRelative(provider.last_ok_at)}
      </td>
      <td className="tight detail" title={formatTimestamp(provider.last_checked_at)}>
        {formatRelative(provider.last_checked_at)}
      </td>
      <td className={`num ${provider.consecutive_failures > 0 ? "metric-bad" : ""}`}>
        {provider.consecutive_failures}
      </td>
      <td className="tight">
        <Link className="button-quiet" to={`/logs?services=${encodeURIComponent(provider.provider)}`}>
          View logs
        </Link>
      </td>
    </tr>
  );
}

/** The two links every subsystem panel offers: its evidence, and its knobs. */
function ServiceLinks({ service, settings }: { service: string; settings?: string }) {
  return (
    <span className="button-row">
      <Link className="button-quiet" to={`/logs?services=${encodeURIComponent(service)}`}>
        View logs
      </Link>
      {settings && (
        <Link className="button-quiet" to="/settings">
          Settings
        </Link>
      )}
    </span>
  );
}
