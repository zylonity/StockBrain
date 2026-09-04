import { useCallback, useEffect, useState } from "react";

export interface PollState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refresh: () => void;
}

/**
 * Fetch on mount and on an interval, with manual refresh.
 *
 * Deliberately small: the dashboard is read-only, so a data-fetching library
 * would add a dependency without adding capability.
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
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
    // `fetcher` is a stable module-level function in every current call site.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick]);

  useEffect(() => {
    if (intervalMs <= 0) return;
    const id = window.setInterval(refresh, intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs, refresh]);

  return { data, error, loading, refresh };
}
