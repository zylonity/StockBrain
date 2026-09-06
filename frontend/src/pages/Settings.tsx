import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../api/client";
import type {
  ControlStateResponse,
  DiscoveryStatus,
  NotificationPreferences,
  SettingGroup,
  SettingView,
  SettingsResponse,
} from "../api/types";
import { Async, EmptyState, PageHeader, RefreshButton } from "../components/Page";
import { StatusPill } from "../components/StatusPill";
import { formatTimestamp } from "../components/formats";
import { usePolling } from "../components/usePolling";

/**
 * What this deployment is configured to do, and the small part of it that can
 * change without a restart.
 *
 * The page is mostly read-only, and that is the design rather than a gap. The
 * risk limits and the four live-execution gates are read by code that runs with
 * no human present; a value that could change underneath a running evaluation
 * is a value two halves of one decision could disagree about. So the server
 * offers no generic settings write, and this page offers no switch that would
 * need a restart to mean anything.
 *
 * What it does offer is legibility: every setting with what it does, what
 * changing it affects, the environment variable that sets it, and whether it is
 * runtime-editable, restart-required, derived or a write-only secret. A secret
 * shows only whether one is present.
 *
 * Three things genuinely are runtime state, and each gets a real control here:
 * the trading pause and kill switch, the discovery hold, and the notification
 * categories.
 */

const MUTABILITY_LABEL: Record<string, string> = {
  RUNTIME: "editable now",
  RESTART_REQUIRED: "restart required",
  READ_ONLY: "derived",
  SECRET: "secret",
};

const MUTABILITY_CLASS: Record<string, string> = {
  RUNTIME: "badge badge-ok",
  RESTART_REQUIRED: "badge",
  READ_ONLY: "badge badge-path-indirect",
  SECRET: "badge badge-warn",
};

export function Settings() {
  const settings = usePolling<SettingsResponse>(api.settings, 120_000);
  const control = usePolling<ControlStateResponse>(api.controlState, 20_000);
  const discovery = usePolling<DiscoveryStatus>(api.discoveryStatus, 30_000);

  return (
    <>
      <PageHeader
        title="Settings"
        subtitle="Everything this deployment is configured to do. Almost all of it is set in the environment and read once at start-up — deliberately, because a limit that could change mid-evaluation is a limit two halves of one decision could disagree about."
        actions={<RefreshButton onClick={settings.refresh} busy={settings.loading} />}
      />

      <div className="banner banner-info">
        <div className="banner-title">How to change something</div>
        <div className="banner-body">
          Rows marked <span className="badge badge-ok">editable now</span> have a
          control on this page and take effect immediately. Everything else is
          set in <code>.env</code> and applied by restarting the application.
          There is no way to change an execution gate or a risk limit over HTTP,
          and that is what makes the gates gates.
        </div>
      </div>

      <TradingControls control={control} />
      <DiscoveryHold discovery={discovery} />
      <NotificationSettings />

      <h2 className="section-title">Configuration</h2>
      <Async state={settings} errorTitle="Settings unavailable" rows={6}>
        {(data) => (
          <>
            {data.groups.map((group) => (
              <SettingsGroup key={group.key} group={group} />
            ))}
            <p className="detail">Read at {formatTimestamp(data.generated_at)}.</p>
          </>
        )}
      </Async>
    </>
  );
}

/* --------------------------------------------------------------------------
 * Runtime controls
 * ----------------------------------------------------------------------- */

function TradingControls({
  control,
}: {
  control: ReturnType<typeof usePolling<ControlStateResponse>>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = useCallback(
    async (run: () => Promise<unknown>) => {
      setBusy(true);
      setError(null);
      try {
        await run();
      } catch (cause) {
        setError(cause instanceof ApiError ? cause.message : String(cause));
      } finally {
        setBusy(false);
        control.refresh();
      }
    },
    [control],
  );

  const paused = control.data?.paused.active ?? false;
  const killed = control.data?.kill_switch.active ?? false;

  return (
    <div className="card">
      <h2>
        Trading control
        <span className="badge badge-ok">editable now</span>
      </h2>
      <p className="setting-description">
        Pausing stops new proposals and every authorization path — web, Telegram
        and automatic. The kill switch does the same and additionally denies any
        future order transmission. Neither closes a position and neither cancels
        a broker order: StockBrain has no order, cancel or amend path. Both are
        stored in PostgreSQL, so they survive a restart and are the same state
        Telegram reads.
      </p>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <div className="spread" style={{ marginTop: 12 }}>
        <span className="row">
          <StatusPill
            status={killed ? "DOWN" : paused ? "DEGRADED" : "HEALTHY"}
            label={killed ? "KILL SWITCH ENGAGED" : paused ? "PAUSED" : "RUNNING"}
          />
          {control.data?.paused.changed_at && paused && (
            <span className="detail">
              since {formatTimestamp(control.data.paused.changed_at)}
            </span>
          )}
        </span>
        <span className="button-row">
          <button onClick={() => void act(() => api.pauseTrading("paused from Settings"))} disabled={busy || paused}>
            Pause
          </button>
          <button onClick={() => void act(() => api.resumeTrading("resumed from Settings"))} disabled={busy || !paused}>
            Resume
          </button>
          <button
            className="button-danger"
            onClick={() => void act(() => api.setKillSwitch(true, "emergency stop from Settings"))}
            disabled={busy || killed}
          >
            Engage kill switch
          </button>
          <button
            onClick={() => void act(() => api.setKillSwitch(false, "released from Settings"))}
            disabled={busy || !killed}
          >
            Release kill switch
          </button>
        </span>
      </div>
      <p className="setting-impact">
        Resuming deliberately does not release the kill switch. If it did, an
        emergency stop would be one routine action away from being undone by
        somebody who only meant to restart normal work.
      </p>
    </div>
  );
}

function DiscoveryHold({
  discovery,
}: {
  discovery: ReturnType<typeof usePolling<DiscoveryStatus>>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const held = discovery.data?.paused ?? false;

  const act = useCallback(
    async (run: () => Promise<unknown>) => {
      setBusy(true);
      setError(null);
      try {
        await run();
      } catch (cause) {
        setError(cause instanceof ApiError ? cause.message : String(cause));
      } finally {
        setBusy(false);
        discovery.refresh();
      }
    },
    [discovery],
  );

  return (
    <div className="card">
      <h2>
        Discovery hold
        <span className="badge badge-ok">editable now</span>
      </h2>
      <p className="setting-description">
        Stops scheduled discovery being enqueued: web searches, the SEC sweep and
        the news backfill. Classification, research, proposals and broker
        reconciliation carry on. This is not the trading pause — holding
        discovery stops the system spending money on new information, pausing
        trading stops it acting on information it already has.
      </p>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <div className="spread" style={{ marginTop: 12 }}>
        <StatusPill
          status={held ? "DISABLED" : "HEALTHY"}
          label={held ? "HELD" : "RUNNING"}
          title={
            held
              ? "Scheduled discovery is not being enqueued. Nothing else is affected."
              : "Scheduled discovery is being enqueued normally."
          }
        />
        <span className="button-row">
          <button onClick={() => void act(() => api.pauseDiscovery("held from Settings"))} disabled={busy || held}>
            Hold discovery
          </button>
          <button onClick={() => void act(() => api.resumeDiscovery("resumed from Settings"))} disabled={busy || !held}>
            Resume discovery
          </button>
          <Link className="button-quiet" to="/discovery" style={{ alignSelf: "center" }}>
            Topics and budgets →
          </Link>
        </span>
      </div>
      <p className="setting-impact">
        Work already claimed by a worker still runs to completion: a paid call
        that has left cannot be unspent, and cancelling it would lose the result
        without recovering the money.
      </p>
    </div>
  );
}

/* --------------------------------------------------------------------------
 * Notification preferences
 * ----------------------------------------------------------------------- */

function NotificationSettings() {
  const preferences = usePolling<NotificationPreferences>(api.notificationPreferences, 0);
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // A save discards any edit the operator had in flight, so clearing here keeps
  // the switches showing what the server actually stored.
  useEffect(() => {
    setPending({});
  }, [preferences.data?.updated_at]);

  const dirty = Object.keys(pending).length > 0;

  const save = useCallback(async () => {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      await api.updateNotificationPreferences(pending);
      setPending({});
      setSaved(true);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setSaving(false);
      preferences.refresh();
    }
  }, [pending, preferences]);

  return (
    <div className="card">
      <h2>
        Telegram notifications
        <span className="badge badge-ok">editable now</span>
      </h2>
      <p className="setting-description">
        Which categories reach the chat. Stored in PostgreSQL and applied
        immediately. Defaults are deliberately quiet: the high-volume stages ship
        switched off, because a channel nobody reads is worse than no channel —
        it looks like coverage.
      </p>

      <Async state={preferences} errorTitle="Notification preferences unavailable" rows={4}>
        {(data) => (
          <>
            {!data.delivery_available && (
              <div className="banner banner-warn">
                <div className="banner-title">Nothing can be delivered right now</div>
                <div className="banner-body">
                  These preferences are saved and will apply as soon as delivery
                  is possible.
                  <ul className="reason-list">
                    {data.blockers.map((blocker) => (
                      <li key={blocker}>{blocker}</li>
                    ))}
                  </ul>
                </div>
              </div>
            )}

            {data.categories.map((category) => {
              const checked = pending[category.category] ?? category.enabled;
              return (
                <label
                  className={`toggle${category.locked ? " toggle-disabled" : ""}`}
                  key={category.category}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={category.locked || saving}
                    onChange={(event) =>
                      setPending((current) => ({
                        ...current,
                        [category.category]: event.target.checked,
                      }))
                    }
                  />
                  <span className="toggle-body">
                    <span className="toggle-label">
                      {category.label}
                      <span className="badge">{category.volume} volume</span>
                      {category.locked && (
                        <span className="badge badge-bad" title="This category cannot be switched off.">
                          always on
                        </span>
                      )}
                    </span>
                    <span className="toggle-description">{category.description}</span>
                  </span>
                </label>
              );
            })}

            <div className="save-bar">
              <button
                type="button"
                className="button-primary"
                onClick={() => void save()}
                disabled={!dirty || saving}
              >
                {saving ? "Saving…" : "Save preferences"}
              </button>
              {dirty && !saving && (
                <button type="button" onClick={() => setPending({})}>
                  Discard changes
                </button>
              )}
              <span className="save-note">
                {error ? (
                  <span className="metric-bad">{error}</span>
                ) : saved && !dirty ? (
                  "Saved."
                ) : data.updated_at ? (
                  `Last changed ${formatTimestamp(data.updated_at)}${
                    data.updated_by ? ` by ${data.updated_by}` : ""
                  }.`
                ) : (
                  "Using the shipped defaults."
                )}
              </span>
            </div>
          </>
        )}
      </Async>
    </div>
  );
}

/* --------------------------------------------------------------------------
 * The configuration catalogue
 * ----------------------------------------------------------------------- */

function SettingsGroup({ group }: { group: SettingGroup }) {
  // Runtime rows have their own controls above; repeating them here as inert
  // text would be a second, weaker copy of a real switch.
  const rows = group.settings.filter((item) => item.mutability !== "RUNTIME");
  return (
    <details className="card" open={group.blockers.length > 0}>
      <summary>{group.title}</summary>
      <p className="setting-description">{group.description}</p>

      {group.warning && (
        <div className="banner banner-warn">
          <div className="banner-body">{group.warning}</div>
        </div>
      )}

      {group.blockers.length > 0 && (
        <div className="banner banner-warn">
          <div className="banner-title">Not currently usable</div>
          <ul className="reason-list">
            {group.blockers.map((blocker) => (
              <li key={blocker}>{blocker}</li>
            ))}
          </ul>
        </div>
      )}

      {rows.length === 0 ? (
        <EmptyState title="Nothing to show here">
          <p>Every setting in this group is a runtime control shown above.</p>
        </EmptyState>
      ) : (
        rows.map((item) => <SettingRow key={item.key} setting={item} />)
      )}
    </details>
  );
}

function SettingRow({ setting }: { setting: SettingView }) {
  return (
    <div className="setting">
      <div>
        <div className="setting-label">{setting.label}</div>
        {setting.env_var && <div className="setting-env">{setting.env_var}</div>}
      </div>
      <div>
        <div className="row">
          <span className="setting-value">
            <SettingValue setting={setting} />
          </span>
          {setting.unit && <span className="faint">{setting.unit}</span>}
          <span className={MUTABILITY_CLASS[setting.mutability] ?? "badge"}>
            {MUTABILITY_LABEL[setting.mutability] ?? setting.mutability}
          </span>
        </div>
        <p className="setting-description">{setting.description}</p>
        {setting.impact && <p className="setting-impact">Effect: {setting.impact}</p>}
      </div>
    </div>
  );
}

/**
 * Renders a value, or the fact that a secret exists.
 *
 * A secret is never sent to this browser — the API reports `configured` and
 * nothing else — so there is no value here to accidentally leak into a
 * screenshot, a bug report or a support thread.
 */
function SettingValue({ setting }: { setting: SettingView }) {
  if (setting.mutability === "SECRET") {
    return setting.configured ? (
      <span className="badge badge-ok" title="A credential is present. Its value is never sent to this page.">
        configured
      </span>
    ) : (
      <span className="badge badge-warn" title="No credential is set for this.">
        not set
      </span>
    );
  }
  if (setting.value === null || setting.value === "") {
    return <span className="faint">not set</span>;
  }
  if (setting.value === "true") return <span className="badge badge-ok">on</span>;
  if (setting.value === "false") return <span className="badge">off</span>;
  return <>{setting.value}</>;
}
