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

import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Portfolio } from "./Portfolio";
import { failWith, portfolioResponse, renderAt, stubFetch } from "../test/harness";

/** A position StockBrain opened, carrying the floors its exit rules would act on. */
const managedPosition = {
  broker_ticker: "AAPL_US_EQ",
  name: "Apple Inc",
  quantity: "2.0000",
  quantity_available: "2.0000",
  average_price: "180.0000",
  current_price: "190.0000",
  ppl: "20.0000",
  currency: "USD",
  last_synced_at: "2026-09-06T11:59:00Z",
  exit: {
    managed: true,
    reason: null,
    hard_stop: "92.00",
    volatility_floor: "119.00",
    trailing_floor: null,
    roi_target_price: "115.00",
    horizon_ends_at: "2026-10-01T00:00:00Z",
    nearest_floor: "119.00",
    nearest_rule: "volatility_stop",
    peak_price: "125.00",
    atr: "2.00",
    horizon: "weeks",
  },
};

/** A position the broker holds but StockBrain has no executed buy behind. */
const unmanagedPosition = {
  broker_ticker: "TSLA_US_EQ",
  name: "Tesla Inc",
  quantity: "1.0000",
  quantity_available: "1.0000",
  average_price: "200.0000",
  current_price: "210.0000",
  ppl: "-5.0000",
  currency: "USD",
  last_synced_at: "2026-09-06T11:59:00Z",
  exit: {
    managed: false,
    reason: "no StockBrain buy behind it",
    hard_stop: null,
    volatility_floor: null,
    trailing_floor: null,
    roi_target_price: null,
    horizon_ends_at: null,
    nearest_floor: null,
    nearest_rule: null,
    peak_price: null,
    atr: null,
    horizon: null,
  },
};

/** A managed position whose rules refuse to act: the broker holds no tradable shares. */
const managedWithoutFloorsPosition = {
  broker_ticker: "MSFT_US_EQ",
  name: "Microsoft Corp",
  quantity: "3.0000",
  quantity_available: "0.0000",
  average_price: "300.0000",
  current_price: "310.0000",
  ppl: "30.0000",
  currency: "USD",
  last_synced_at: "2026-09-06T11:59:00Z",
  exit: {
    managed: true,
    reason: "no tradable shares — held in a pie or not yet settled",
    hard_stop: null,
    volatility_floor: null,
    trailing_floor: null,
    roi_target_price: null,
    horizon_ends_at: null,
    nearest_floor: null,
    nearest_rule: null,
    peak_price: null,
    atr: null,
    horizon: null,
  },
};

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

  it("shows each position's exit floors, and says when one is not managed", async () => {
    stubFetch({
      "GET /api/v1/portfolio": {
        ...portfolioResponse,
        position_count: 2,
        positions: [managedPosition, unmanagedPosition],
      },
    });
    renderAt(<Portfolio />, "/portfolio");

    const managed = (await screen.findByText("Apple Inc")).closest("tr") as HTMLElement;
    expect(within(managed).getByText("119.00")).toBeInTheDocument();
    expect(within(managed).getByText(/volatility/)).toBeInTheDocument();
    expect(within(managed).getByText(/92\.00/)).toBeInTheDocument();

    const unmanaged = screen.getByText("Tesla Inc").closest("tr") as HTMLElement;
    expect(within(unmanaged).getByText(/not managed/)).toBeInTheDocument();
    expect(within(unmanaged).getByText(/no StockBrain buy behind it/)).toBeInTheDocument();
  });

  it("says floors are unavailable for a managed position with no floors", async () => {
    stubFetch({
      "GET /api/v1/portfolio": {
        ...portfolioResponse,
        position_count: 1,
        positions: [managedWithoutFloorsPosition],
      },
    });
    renderAt(<Portfolio />, "/portfolio");

    const row = (await screen.findByText("Microsoft Corp")).closest("tr") as HTMLElement;
    expect(within(row).getByText(/floors unavailable/)).toBeInTheDocument();
    expect(within(row).getByText(/no tradable shares/)).toBeInTheDocument();
    // The normal branch's stacked floor list must not be invented here.
    expect(within(row).queryByText(/stop/)).toBeNull();
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

  it("can queue a confirmed re-review for one held stock", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetchStub = stubFetch({
      "GET /api/v1/portfolio": portfolioResponse,
      "POST /api/v1/portfolio/review": {
        requested: { AAPL_US_EQ: "00000000-0000-0000-0000-000000000001" },
        skipped: {},
      },
    });
    renderAt(<Portfolio />, "/portfolio");

    const row = (await screen.findByText("Apple Inc")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Re-review" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Queued AAPL_US_EQ");
    expect(fetchStub.callsTo("/api/v1/portfolio/review")[0]?.body).toEqual({
      ticker: "AAPL_US_EQ",
    });
  });
});
