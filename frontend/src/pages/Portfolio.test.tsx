/**
 * Portfolio.
 *
 * The whole design question here is honesty about age. The page reads
 * StockBrain's stored mirror rather than the broker, so the figures can be
 * older than the freshness the risk engine requires for sizing — and a cash
 * balance shown next to a live proposal without that caveat invites exactly the
 * wrong arithmetic. There is also no client-side arithmetic at all: every
 * figure is the server's own decimal string, grouped for reading.
 */

import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";

import { Portfolio } from "./Portfolio";
import { failWith, portfolioResponse, renderAt, stubFetch } from "../test/harness";

describe("Portfolio", () => {
  it("renders the broker's own figures without recomputing them", async () => {
    stubFetch({ "GET /api/v1/portfolio": portfolioResponse });
    renderAt(<Portfolio />, "/portfolio");

    // 1000.0000 GBP arrives as a string and is grouped, not parsed.
    expect(await screen.findByText("1,000.00 GBP")).toBeInTheDocument();
    expect(screen.getByText("400.00 GBP")).toBeInTheDocument();
    // A negative result is coloured, and keeps its sign.
    const result = screen.getByText("-12.34 GBP");
    expect(result).toHaveClass("metric-bad");
  });

  it("shows a position with the listing it belongs to", async () => {
    stubFetch({ "GET /api/v1/portfolio": portfolioResponse });
    renderAt(<Portfolio />, "/portfolio");

    const row = (await screen.findByText("Apple Inc")).closest("tr");
    expect(within(row as HTMLElement).getByText(/AAPL_US_EQ/)).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("190.00")).toBeInTheDocument();
  });

  it("says the snapshot is fresh enough to size against when it is", async () => {
    stubFetch({ "GET /api/v1/portfolio": portfolioResponse });
    renderAt(<Portfolio />, "/portfolio");
    expect(
      await screen.findByText(/Fresh enough for the risk engine to size against/),
    ).toBeInTheDocument();
  });

  it("warns unmissably when the snapshot is older than the sizing limit", async () => {
    stubFetch({
      "GET /api/v1/portfolio": {
        ...portfolioResponse,
        stale: true,
        captured_at: "2026-09-06T08:00:00Z",
      },
    });
    renderAt(<Portfolio />, "/portfolio");

    expect(await screen.findByText("Snapshot is stale")).toBeInTheDocument();
    expect(
      screen.getByText(/A proposal cannot be authorized on a snapshot this old/),
    ).toBeInTheDocument();
    // The "fresh enough to size against" reassurance must be gone, not merely
    // accompanied by a warning.
    expect(screen.queryByText(/Fresh enough for the risk engine/)).toBeNull();
  });

  it("explains an absent snapshot instead of showing zeroes", async () => {
    // Rendering "0.00" for cash nobody has read would be a number an operator
    // could act on.
    stubFetch({
      "GET /api/v1/portfolio": {
        ...portfolioResponse,
        available: false,
        reason: "No broker account snapshot has been captured yet.",
        positions: [],
        total_value: null,
      },
    });
    renderAt(<Portfolio />, "/portfolio");

    expect(await screen.findByText("No broker snapshot yet")).toBeInTheDocument();
    expect(screen.queryByText("0.00 GBP")).toBeNull();
    expect(screen.getByRole("link", { name: "Broker settings" })).toBeInTheDocument();
  });

  it("distinguishes cash-only from no snapshot", async () => {
    stubFetch({
      "GET /api/v1/portfolio": { ...portfolioResponse, positions: [], position_count: 0 },
    });
    renderAt(<Portfolio />, "/portfolio");

    expect(await screen.findByText("No open positions")).toBeInTheDocument();
    // The summary is still there: the account has money in it.
    expect(screen.getByText("1,000.00 GBP")).toBeInTheDocument();
  });

  it("shows the server's own reason when the read fails", async () => {
    stubFetch({ "GET /api/v1/portfolio": failWith(503, "database unavailable") });
    renderAt(<Portfolio />, "/portfolio");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Portfolio unavailable");
    expect(alert).toHaveTextContent("database unavailable");
  });

  it("never calls the broker", async () => {
    const fetchStub = stubFetch({ "GET /api/v1/portfolio": portfolioResponse });
    renderAt(<Portfolio />, "/portfolio");
    await screen.findByText("Apple Inc");
    // One read of StockBrain's own mirror. The broker's account endpoint allows
    // one request every five seconds and this page must not spend it.
    expect(fetchStub.calls.every((call) => call.url.startsWith("/api/"))).toBe(true);
    expect(fetchStub.callsTo("/api/v1/portfolio")).toHaveLength(1);
  });
});
