import type { ProviderStatus } from "../api/types";

export function StatusPill({ status }: { status: ProviderStatus }) {
  return (
    <span className={`status status-${status}`} title={status}>
      {status}
    </span>
  );
}
