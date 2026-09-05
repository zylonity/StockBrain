import type { ControlStateResponse } from "../api/types";
import { StatusPill } from "./StatusPill";

function formatMoment(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toISOString().replace("T", " ").slice(0, 16) + "Z";
}

/**
 * Shown only while trading is halted, and then unmissable.
 *
 * The state comes from PostgreSQL, so it is the same state the Telegram bot
 * reports and the same state that survives a restart. The banner repeats what
 * a halt does *not* do, because that is the question somebody asks in the
 * moment they engage one.
 */
export function ControlBanner({ control }: { control: ControlStateResponse }) {
  if (!control.trading_halted) return null;
  const killed = control.kill_switch.active;
  const flag = killed ? control.kill_switch : control.paused;
  return (
    <div className="banner banner-live">
      <div className="banner-title">
        <span>{killed ? "EMERGENCY KILL SWITCH ENGAGED" : "TRADING PAUSED"}</span>
        <StatusPill status="DOWN" />
        <span className="faint mono">
          since {formatMoment(flag.changed_at)} · {flag.source ?? "unknown source"}
        </span>
      </div>
      <div className="banner-body">{control.notice}</div>
      <ul>
        {control.blockers.map((blocker) => (
          <li key={blocker}>{blocker}</li>
        ))}
      </ul>
    </div>
  );
}
