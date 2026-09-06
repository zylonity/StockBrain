/**
 * The shared page furniture: headers, and the three states every panel has.
 *
 * Loading, empty and error were previously spelled a different way on every
 * page -- "Loading…", "Loading research…", a bare `<p className="muted">`, a
 * dashed placeholder, nothing at all. That inconsistency is not cosmetic: an
 * operator learns what a blank panel means, and a page that renders emptiness
 * as silence teaches them that silence is fine.
 *
 * So there are exactly three, they always say what to do next, and an empty
 * state is never the same shape as an error.
 */

import type { ReactNode } from "react";

export function PageHeader({
  title,
  subtitle,
  actions,
  children,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <header className="page-head">
      <div className="page-head-text">
        <h1 className="page-title">{title}</h1>
        {subtitle && <p className="page-subtitle">{subtitle}</p>}
        {children}
      </div>
      {actions && <div className="page-head-actions">{actions}</div>}
    </header>
  );
}

/**
 * A refresh button that reports what it is doing.
 *
 * Disabled while in flight rather than hidden: a control that disappears under
 * the pointer is worse than one that greys out.
 */
export function RefreshButton({
  onClick,
  busy,
  label = "Refresh",
}: {
  onClick: () => void;
  busy?: boolean;
  label?: string;
}) {
  return (
    <button onClick={onClick} disabled={busy} aria-busy={busy || undefined}>
      {busy ? "Refreshing…" : label}
    </button>
  );
}

/**
 * A failed request, shown with the server's own words.
 *
 * The status is part of the message because the useful cases are specific: a
 * 401 means the session lapsed, a 503 usually names a missing credential, and a
 * 409 from a proposal route is the state machine answering rather than failing.
 */
export function ErrorState({
  title = "Could not load this",
  error,
  onRetry,
}: {
  title?: string;
  error: string;
  onRetry?: () => void;
}) {
  return (
    <div className="error" role="alert">
      <span className="error-title">{title}</span>
      <span className="error-detail">{error}</span>
      {onRetry && (
        <div className="empty-actions">
          <button onClick={onRetry}>Try again</button>
        </div>
      )}
    </div>
  );
}

/**
 * Nothing here yet -- and why, and what would change it.
 *
 * "No events" is not information. "No events: discovery providers need
 * credentials before anything arrives" is, and it points at the page that fixes
 * it.
 */
export function EmptyState({
  title,
  children,
  actions,
}: {
  title: string;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="empty">
      <p className="empty-title">{title}</p>
      {children}
      {actions && <div className="empty-actions">{actions}</div>}
    </div>
  );
}

/** Placeholder rows that keep the layout still while the first load lands. */
export function LoadingRows({ rows = 3, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-live="polite">
      <span className="visually-hidden">{label}</span>
      {Array.from({ length: rows }, (_, index) => (
        <div className="skeleton skeleton-row" key={index} aria-hidden="true" />
      ))}
    </div>
  );
}

/**
 * The three states, resolved once, in the same order everywhere.
 *
 * Error first: a stale success rendered next to a failed refresh is how a page
 * shows an operator numbers that are no longer true.
 */
export function Async<T>({
  state,
  empty,
  children,
  rows,
  errorTitle,
}: {
  state: { data: T | null; error: string | null; loading: boolean; refresh: () => void };
  empty?: (data: T) => ReactNode;
  children: (data: T) => ReactNode;
  rows?: number;
  errorTitle?: string;
}) {
  if (state.error && state.data === null) {
    return (
      <ErrorState
        title={errorTitle ?? "Could not load this"}
        error={state.error}
        onRetry={state.refresh}
      />
    );
  }
  if (state.data === null) {
    return <LoadingRows rows={rows ?? 3} />;
  }
  const emptyView = empty?.(state.data);
  return (
    <>
      {/* A refresh that failed while data is on screen: the data stays, and the
          page says it may be stale rather than replacing it with an error. */}
      {state.error && (
        <div className="banner banner-warn">
          <div className="banner-title">Showing the last successful read</div>
          <div className="banner-body">{state.error}</div>
        </div>
      )}
      {emptyView ?? children(state.data)}
    </>
  );
}

/** A table that scrolls sideways instead of making the page do it. */
export function TableWrap({ children }: { children: ReactNode }) {
  return <div className="table-wrap">{children}</div>;
}
