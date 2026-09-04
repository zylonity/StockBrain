import { NavLink, Outlet } from "react-router-dom";

/**
 * Navigation includes the pages that later phases fill in. They are rendered
 * as explicitly disabled rather than hidden, so the build state of the system
 * is visible instead of implied.
 */
const NAV = [
  { to: "/", label: "Dashboard", enabled: true },
  { to: "/health", label: "System health", enabled: true },
  { to: "/events", label: "Events", enabled: false },
  { to: "/research", label: "Research", enabled: false },
  { to: "/proposals", label: "Proposals", enabled: false },
  { to: "/portfolio", label: "Portfolio", enabled: false },
  { to: "/settings", label: "Settings", enabled: false },
] as const;

export function Layout() {
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          StockBrain
          <span className="version">v0.1.0</span>
        </div>
        <nav className="nav">
          {NAV.map((item) =>
            item.enabled ? (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) => (isActive ? "active" : "")}
              >
                {item.label}
              </NavLink>
            ) : (
              <a key={item.to} aria-disabled="true" title="Not implemented yet">
                {item.label}
              </a>
            ),
          )}
        </nav>
      </header>
      <main>
        <Outlet />
      </main>
      <footer className="footer">
        <span>
          Every broker order requires explicit two-stage human approval. No
          automatic retries on broker mutations.
        </span>
        <span className="mono">phase 1 · skeleton &amp; state model</span>
      </footer>
    </div>
  );
}
