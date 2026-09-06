import { useCallback, useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";

import { api } from "../api/client";
import { usePolling } from "./usePolling";

/**
 * The application shell.
 *
 * Two things live here rather than on a page, because they must be true
 * everywhere:
 *
 * **The broker environment.** Demo and live differ by one configuration string
 * and an irreversible consequence. The chip is in the top bar on every route,
 * and it is red when it is real.
 *
 * **Whether trading is halted.** A pause or a kill switch can be engaged from
 * Telegram at any moment. An operator reading the proposals page must not have
 * to navigate to System health to discover that nothing can be authorized.
 *
 * Navigation lists only routes that exist. The previous version rendered
 * "Portfolio" and "Settings" as permanently disabled links to advertise the
 * build state -- which stopped being informative the moment both pages shipped,
 * and was never information an operator needed.
 */
const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/events", label: "Events" },
  { to: "/research", label: "Research" },
  { to: "/proposals", label: "Proposals" },
  { to: "/portfolio", label: "Portfolio" },
  { to: "/discovery", label: "Discovery" },
  { to: "/instruments", label: "Instruments" },
  { to: "/health", label: "Health" },
  { to: "/logs", label: "Logs" },
  { to: "/settings", label: "Settings" },
] as const;

export function Layout() {
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();

  // Polled rather than read once: both can change from Telegram, from another
  // browser, or by a restart picking up a persisted halt.
  const execution = usePolling(api.executionStatus, 120_000);
  const control = usePolling(api.controlState, 20_000);
  // Polled slowly and only for the build identity; it changes on deploy.
  const liveness = usePolling(api.liveness, 600_000);

  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      // Reload rather than route: the auth gate is above the router, and the
      // cleanest way back to a login form is to let it re-evaluate from
      // scratch. Runs even if the request failed -- the cookie may be gone.
      window.location.assign("/");
    }
  }, []);

  const live = execution.data?.broker_environment === "live";
  const halted = control.data?.trading_halted ?? false;
  const killed = control.data?.kill_switch.active ?? false;

  return (
    <div className={`app${navOpen ? " nav-open" : ""}`}>
      <header className="topbar">
        <Link className="brand" to="/">
          StockBrain
          {/* The running build. The execution *mode* used to sit here, which
              read as a version string and duplicated the banner below it. */}
          <span className="version">{liveness.data ? `v${liveness.data.version}` : ""}</span>
        </Link>

        <button
          type="button"
          className="nav-toggle button-quiet"
          aria-expanded={navOpen}
          aria-controls="primary-navigation"
          onClick={() => setNavOpen((open) => !open)}
        >
          {navOpen ? "Close" : "Menu"}
        </button>

        <div className="topbar-actions">
          {halted && (
            <Link
              className="env-chip env-chip-halted"
              to="/health"
              title={
                killed
                  ? "The emergency kill switch is engaged: no authorization and no order transmission."
                  : "Trading is paused: no new proposals and no authorization."
              }
            >
              {killed ? "KILL SWITCH" : "PAUSED"}
            </Link>
          )}
          {execution.data && (
            <Link
              className={`env-chip ${live ? "env-chip-live" : "env-chip-demo"}`}
              to="/health"
              title={
                live
                  ? "Trading 212 live environment. Orders transmitted here are real."
                  : "Trading 212 demo environment. Orders here are paper."
              }
            >
              {live ? "LIVE · REAL MONEY" : "DEMO"}
            </Link>
          )}
          <button type="button" className="button-quiet" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>

        <nav className="nav" id="primary-navigation" aria-label="Primary">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={"end" in item ? item.end : false}
              className={({ isActive }) => (isActive ? "active" : "")}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </header>

      <main>
        <Outlet />
      </main>

      <footer className="footer">
        <span>
          Authorizing a proposal and transmitting an order are separately gated.
          Every order is sent at most once, and a broker mutation is never
          retried: an unknown outcome is reconciled, never resent.
        </span>
        <span className="mono">
          {execution.data
            ? `${execution.data.broker} · ${execution.data.broker_environment}`
            : ""}
        </span>
      </footer>
    </div>
  );
}
