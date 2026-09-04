/** Shared display formatting. Presentation only — no financial or risk logic. */

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toISOString().replace("T", " ").slice(0, 19) + "Z";
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return "—";
  const seconds = (Date.now() - new Date(value).getTime()) / 1000;
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(2);
}

/** Hostname only, for compact source attribution. */
export function hostOf(url: string | null | undefined): string {
  if (!url) return "—";
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url.slice(0, 40);
  }
}
