/**
 * The Logs page, and the health-board link that lands on it.
 *
 * The flow that matters is not "logs render" -- it is "a DOWN provider on the
 * health board is one click from its own log lines". That link is only a link
 * because the service keys are shared, so the test asserts the whole hop:
 * health row → URL → filtered request → filtered table.
 */

import { describe, expect, it } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { Logs } from "./Logs";
import { SystemHealth } from "./SystemHealth";
import {
  controlState,
  discoveryStatus,
  executionSummary,
  failWith,
  fxStatus,
  healthResponse,
  logFacets,
  logsResponse,
  providersResponse,
  renderAt,
  stubFetch,
  telegramStatus,
  webSecurity,
} from "../test/harness";

function logRoutes(overrides: Record<string, object> = {}) {
  return {
    "GET /api/v1/system/logs": logsResponse,
    "GET /api/v1/system/logs/facets": logFacets,
    ...overrides,
  };
}

describe("Logs", () => {
  it("renders entries with their structured fields, not raw JSON", async () => {
    stubFetch(logRoutes());
    renderAt(<Logs />, "/logs");

    expect(await screen.findByText("brave_rate_limited")).toBeInTheDocument();
    // The fields are the useful part of a structured log; a JSON blob hides them.
    expect(screen.getByText("http_status=")).toBeInTheDocument();
    expect(screen.getByText("429")).toBeInTheDocument();
  });

  it("says what the buffer holds and since when", async () => {
    stubFetch(logRoutes());
    renderAt(<Logs />, "/logs");

    // A page that silently showed the last N lines of a longer incident would
    // be lying about coverage.
    expect(await screen.findByText(/2 of 4,000 entries/)).toBeInTheDocument();
    expect(screen.getByText(/A restart clears it/)).toBeInTheDocument();
  });

  it("turns URL parameters into a narrowed request", async () => {
    const fetchStub = stubFetch(logRoutes());
    renderAt(<Logs />, "/logs?services=brave&min_level=warning");

    await screen.findByText("brave_rate_limited");
    const request = fetchStub.callsTo("/api/v1/system/logs")[0];
    expect(request?.url).toContain("services=brave");
    expect(request?.url).toContain("min_level=warning");
  });

  it("keeps a service from the URL selectable even when the buffer has no such entry", async () => {
    // Arriving from a health-board link for a provider that has not logged
    // anything must not silently drop the filter.
    stubFetch(logRoutes({ "GET /api/v1/system/logs/facets": { services: {}, categories: {}, levels: {} } }));
    renderAt(<Logs />, "/logs?services=trading212");

    await waitFor(() =>
      expect(screen.getByLabelText("Filter by service")).toHaveValue("trading212"),
    );
  });

  it("clicking a service narrows to it", async () => {
    const fetchStub = stubFetch(logRoutes());
    renderAt(<Logs />, "/logs");
    await screen.findByText("brave_rate_limited");

    await userEvent.click(screen.getByTitle("Show only brave"));

    await waitFor(() => {
      const urls = fetchStub.callsTo("/api/v1/system/logs").map((call) => call.url);
      expect(urls.some((url) => url.includes("services=brave"))).toBe(true);
    });
  });

  it("distinguishes an empty buffer from an over-narrow filter", async () => {
    stubFetch(
      logRoutes({
        "GET /api/v1/system/logs": { ...logsResponse, entries: [], total: 0, stored: 0 },
      }),
    );
    const { unmount } = renderAt(<Logs />, "/logs");
    expect(await screen.findByText("No log entries yet")).toBeInTheDocument();
    unmount();

    stubFetch(
      logRoutes({
        "GET /api/v1/system/logs": { ...logsResponse, entries: [], total: 0 },
      }),
    );
    renderAt(<Logs />, "/logs?search=nothing");
    expect(await screen.findByText("Nothing matches these filters")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Clear filters" }).length).toBeGreaterThan(0);
  });

  it("says so when capture is switched off, rather than looking broken", async () => {
    stubFetch(
      logRoutes({
        "GET /api/v1/system/logs": {
          ...logsResponse,
          enabled: false,
          entries: [],
          total: 0,
          stored: 0,
          capacity: 0,
        },
      }),
    );
    renderAt(<Logs />, "/logs");
    expect(await screen.findByText("Log capture is switched off")).toBeInTheDocument();
  });

  it("shows the server's own reason when the request fails", async () => {
    stubFetch(logRoutes({ "GET /api/v1/system/logs": failWith(503, "the log buffer is unavailable") }));
    renderAt(<Logs />, "/logs");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Logs unavailable");
    expect(alert).toHaveTextContent("the log buffer is unavailable");
  });
});

describe("health → filtered logs", () => {
  it("links a failing provider straight to its own log lines", async () => {
    const fetchStub = stubFetch({
      "GET /api/health/providers": providersResponse,
      "GET /api/health": healthResponse,
      "GET /api/v1/system/control": controlState,
      "GET /api/v1/system/telegram": telegramStatus,
      "GET /api/v1/execution/status": executionSummary,
      "GET /api/v1/discovery/status": discoveryStatus,
      "GET /api/v1/system/fx": fxStatus,
      "GET /api/v1/system/web-security": webSecurity,
      ...logRoutes(),
    });

    render(
      <MemoryRouter initialEntries={["/health"]}>
        <Routes>
          <Route path="/health" element={<SystemHealth />} />
          <Route path="/logs" element={<Logs />} />
        </Routes>
      </MemoryRouter>,
    );

    // The DOWN provider's row, and its own "View logs" action.
    const row = (await screen.findByText("brave")).closest("tr");
    expect(row).not.toBeNull();
    const link = within(row as HTMLElement).getByRole("link", { name: "View logs" });
    expect(link).toHaveAttribute("href", "/logs?services=brave");

    await userEvent.click(link);

    await screen.findByText("brave_rate_limited");
    const request = fetchStub.callsTo("/api/v1/system/logs").at(-1);
    expect(request?.url).toContain("services=brave");
  });
});
