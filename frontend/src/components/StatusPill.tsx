import type { ProviderStatus } from "../api/types";
import { STATUS_MEANING } from "./statuses";

export function StatusPill({
  status,
  label,
  title,
}: {
  status: ProviderStatus;
  label?: string;
  title?: string;
}) {
  return (
    <span
      className={`status status-${status}`}
      title={title ?? STATUS_MEANING[status] ?? status}
    >
      {label ?? status}
    </span>
  );
}

/**
 * The legend, rendered wherever a board of statuses is.
 *
 * Duplicating the meanings next to the board rather than hiding them behind a
 * tooltip: a tooltip is invisible on a phone, which is where an alert is read.
 */
export function StatusLegend({ statuses }: { statuses?: ProviderStatus[] }) {
  const shown =
    statuses ??
    (["HEALTHY", "DEGRADED", "BUDGET_EXHAUSTED", "DOWN", "DISABLED", "UNKNOWN"] as ProviderStatus[]);
  return (
    <div className="status-legend">
      {shown.map((status) => (
        <StatusLegendRow key={status} status={status} />
      ))}
    </div>
  );
}

function StatusLegendRow({ status }: { status: ProviderStatus }) {
  return (
    <>
      <StatusPill status={status} title={status} />
      <span>{STATUS_MEANING[status]}</span>
    </>
  );
}
