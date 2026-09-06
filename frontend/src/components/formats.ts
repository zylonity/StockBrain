/** Shared display formatting. Presentation only — no financial or risk logic.
 *
 * Two rules hold everywhere in this file:
 *
 * **No arithmetic on money.** Decimals arrive from the API as strings because
 * that is what the server persisted and what the decision was made on. They are
 * padded and grouped for reading, never parsed into a float and re-rendered: a
 * quantity that displays differently from the one the risk engine sized is a
 * quantity nobody can reconcile.
 *
 * **Absent is "—", not "0".** A missing spread and a zero spread mean opposite
 * things, and rendering both as `0` is how a page reports a book it never saw.
 */

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return parsed.toISOString().replace("T", " ").slice(0, 19) + "Z";
}

/** Date and minute only, for a column where seconds are noise. */
export function formatMoment(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return parsed.toISOString().replace("T", " ").slice(0, 16) + "Z";
}

/**
 * Wall-clock time in the viewer's own zone, for scanning a log stream.
 *
 * The date is added only when the entry is not from today. A buffer holding
 * several days of events would otherwise show two "09:14:02" rows that are
 * twenty-four hours apart and look adjacent.
 */
export function formatClock(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  const clock = parsed.toLocaleTimeString(undefined, { hour12: false });
  const today = new Date();
  const sameDay =
    parsed.getFullYear() === today.getFullYear() &&
    parsed.getMonth() === today.getMonth() &&
    parsed.getDate() === today.getDate();
  if (sameDay) return clock;
  return `${parsed.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${clock}`;
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  const seconds = (Date.now() - parsed.getTime()) / 1000;
  if (seconds < 0) return `in ${formatDuration(-seconds)}`;
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

export function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(2);
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

/**
 * A server-supplied decimal string, grouped for reading.
 *
 * The digits are never recomputed: the integer part gets thousands separators
 * and the fraction is padded or left exactly as it arrived.
 */
export function formatDecimal(
  value: string | null | undefined,
  { places }: { places?: number } = {},
): string {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value).trim();
  const match = /^(-?)(\d+)(?:\.(\d*))?$/.exec(text);
  if (!match) return text;
  const [, sign = "", whole = "0", fraction = ""] = match;
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  let decimals = fraction;
  if (places !== undefined) {
    decimals = fraction.slice(0, places).padEnd(places, "0");
  }
  return decimals ? `${sign}${grouped}.${decimals}` : `${sign}${grouped}`;
}

/**
 * A share quantity: grouped, with the storage padding removed.
 *
 * `Numeric(28, 10)` arrives as `"2.0000000000"`, and printing that in an order
 * line reads as ten digits of precision nobody has. Trailing zeros are stripped
 * and nothing is rounded, so `0.5` stays `0.5` and `2.0000000001` keeps its last
 * digit rather than being quietly turned into `2`.
 */
export function formatQuantity(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value).trim();
  if (!/^-?\d+(\.\d*)?$/.test(text)) return text;
  const trimmed = text.includes(".") ? text.replace(/0+$/, "").replace(/\.$/, "") : text;
  return formatDecimal(trimmed);
}

/** A money amount with its currency, or an honest dash. */
export function formatMoney(
  value: string | null | undefined,
  currency: string | null | undefined,
  { places = 2 }: { places?: number } = {},
): string {
  const amount = formatDecimal(value, { places });
  if (amount === "—") return "—";
  return currency ? `${amount} ${currency}` : amount;
}

/** Whether a server-supplied decimal string is negative, for colouring a P/L. */
export function isNegative(value: string | null | undefined): boolean {
  return typeof value === "string" && value.trim().startsWith("-");
}

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString();
}

/** A USD cost from the API, which arrives as a string or a number. */
export function formatUsd(value: string | number | null | undefined, digits = 4): string {
  if (value === null || value === undefined || value === "") return "—";
  const amount = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(amount)) return String(value);
  return `$${amount.toFixed(digits)}`;
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

/** Turn an enum-shaped value into something readable without losing it. */
export function humanise(value: string | null | undefined): string {
  if (!value) return "—";
  const words = value.replace(/_/g, " ").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}
