import type { ExecutionStatusResponse } from "../api/types";
import { StatusPill } from "./StatusPill";

/**
 * Permanent, unavoidable statement of the system's execution posture.
 *
 * Shown on every page: the difference between demo and live must never require
 * the operator to go looking for it.
 */
export function ExecutionBanner({ status }: { status: ExecutionStatusResponse }) {
  const live = status.live_execution_permitted;
  return (
    <div className={`banner ${live ? "banner-live" : "banner-safe"}`}>
      <div className="banner-title">
        <span>
          Broker: {status.broker} — {status.broker_environment.toUpperCase()}
        </span>
        <StatusPill status={live ? "DEGRADED" : "DISABLED"} />
        <span className="faint mono">execution_mode={status.execution_mode}</span>
      </div>
      <div className="banner-body">{status.notice}</div>
      {status.blockers.length > 0 && (
        <ul>
          {status.blockers.map((blocker) => (
            <li key={blocker}>{blocker}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
