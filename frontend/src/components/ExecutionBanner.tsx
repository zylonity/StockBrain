import type { ExecutionStatusResponse } from "../api/types";
import { StatusPill } from "./StatusPill";

/**
 * A permanent statement of the system's execution posture.
 *
 * The difference between demo and live must never require the operator to go
 * looking for it. The top bar carries a chip on every route; this is the fuller
 * statement, shown on the pages where a decision gets made.
 *
 * The status pill is deliberately *not* green in either state. Demo is not
 * "healthy", it is switched off; live is not "unhealthy", it is dangerous. The
 * two states an operator must never confuse are given two different colours and
 * two different words rather than one axis they might read as a quality score.
 */
export function ExecutionBanner({ status }: { status: ExecutionStatusResponse }) {
  const live = status.live_execution_permitted;
  return (
    <div className={`banner ${live ? "banner-live" : "banner-safe"}`}>
      <div className="banner-title">
        <span>
          Broker: {status.broker} — {status.broker_environment.toUpperCase()}
        </span>
        <StatusPill
          status={live ? "DEGRADED" : "DISABLED"}
          label={live ? "LIVE EXECUTION PERMITTED" : "LIVE EXECUTION DISABLED"}
          title={
            live
              ? "Every gate is open: an authorized proposal can reach the real broker."
              : "At least one gate is closed, so no order can reach the live environment."
          }
        />
        <span className="faint mono">mode {status.execution_mode}</span>
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
