import { useCallback, useEffect, useState } from "react";

export interface PollState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** When the last successful read landed, so a page can say how old it is. */
  updatedAt: number | null;
  refresh: () => void;
}

/**
 * Fetch on mount and on an interval, with manual refresh.
 *
 * Deliberately small: most of this application is read-only, so a data-fetching
 * library would add a dependency without adding capability.
 *
 * Three properties earn their complexity:
 *
 * **A failed refresh does not erase good data.** `data` is kept and `error` is
 * set alongside it, so the page can show the last successful read with a
 * "possibly stale" note instead of replacing a full screen of numbers with one
 * error line.
 *
 * **A hidden tab does not poll.** A dashboard left open in a background tab for
 * a day was making thousands of requests, some of which probe a provider. The
 * interval pauses on `visibilitychange` and fires once immediately on return,
 * so coming back to the tab shows current data rather than yesterday's.
 *
 * **A changed fetcher refetches immediately.** Call sites build closures over
 * filter state, so the fetcher's identity *is* the query. The previous version
 * depended only on an internal tick with an eslint-disable and a comment
 * claiming every call site passed a stable module-level function -- which was
 * not true of the events page, so changing a filter there did nothing until the
 * twenty-second poll happened to fire. The fetcher is now a real dependency,
 * which means every call site must pass a stable reference: a module-level
 * function, an `api.*` method, or a `useCallback`. An inline arrow would
 * refetch on every render, and the lint rule now says so.
 */
export function usePolling<T>(fetcher: () => Promise<T>, intervalMs: number): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetcher()
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
        setUpdatedAt(Date.now());
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        setError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tick, fetcher]);

  useEffect(() => {
    if (intervalMs <= 0) return;
    let timer: number | undefined;

    const start = () => {
      window.clearInterval(timer);
      timer = window.setInterval(refresh, intervalMs);
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        window.clearInterval(timer);
        return;
      }
      // Refresh immediately on return: the whole point of coming back to the
      // tab is to see now, not to wait out the rest of an interval.
      refresh();
      start();
    };

    if (document.visibilityState !== "hidden") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [intervalMs, refresh]);

  return { data, error, loading, updatedAt, refresh };
}

/**
 * Re-render on a timer so relative times stay honest.
 *
 * "12s ago" that stays "12s ago" for five minutes is worse than no timestamp:
 * it is a claim about freshness that is actively wrong.
 */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (intervalMs <= 0) return;
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}
