import type { EventStatus, SourceCategory } from "../api/types";

/**
 * Source trust category. The ordering of authority is a server-side lookup
 * table; this only renders the label it is given.
 */
export function CategoryBadge({
  category,
}: {
  category: SourceCategory | string | null;
}) {
  if (!category) return <span className="faint">—</span>;
  return (
    <span className={`badge badge-cat-${category}`} title={`Source category: ${category}`}>
      {category}
    </span>
  );
}

export function EventStatusBadge({ status }: { status: EventStatus | string }) {
  return <span className={`badge badge-event-${status}`}>{status}</span>;
}

export function ProviderBadge({ provider }: { provider: string }) {
  return <span className={`badge badge-provider-${provider}`}>{provider}</span>;
}
