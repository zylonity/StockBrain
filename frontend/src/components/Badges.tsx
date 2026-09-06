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

const EVENT_STATUS_MEANING: Record<string, string> = {
  NEW: "Ingested. Nothing has judged it yet.",
  CLASSIFYING: "The classifier is reading it now.",
  CLASSIFIED: "Judged relevant to public equities, but not promoted to research.",
  CLASSIFICATION_FAILED: "The classifier could not produce a valid answer. See the event for why.",
  IRRELEVANT: "Read and dismissed. The pipeline working, not a gap.",
  CANDIDATE: "Cleared every threshold and is queued for paid research.",
  RESEARCHING: "A research run is in progress.",
  RESEARCHED: "Research finished. Any thesis it published is on the Research page.",
  ARCHIVED: "Closed out; no further work will be done on it.",
};

export function EventStatusBadge({ status }: { status: EventStatus | string }) {
  return (
    <span className={`badge badge-event-${status}`} title={EVENT_STATUS_MEANING[status] ?? status}>
      {status}
    </span>
  );
}

/** Which discovery source surfaced a row. Lowercased: a wall of shouting
 *  provider names competes with the headline it is attached to. */
export function ProviderBadge({ provider }: { provider: string }) {
  return (
    <span className={`badge badge-provider-${provider}`} title={`Discovered by ${provider}`}>
      {provider.toLowerCase()}
    </span>
  );
}
