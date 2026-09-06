/**
 * The dashboard.
 *
 * It answers one question -- "is anything waiting for me, and is anything
 * wrong?" -- so the tests are about the two things that must never require
 * navigation to discover: a halt, and an order whose state is unknown.
 *
 * The rest of the page is summaries with links; a summary that duplicated a
 * specialist page would be a second copy to keep in sync.
 */

import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";

import { Dashboard } from "./Dashboard";
import {
  controlState,
  discoveryStatus,
  emptyProposals,
  executionStatus,
  executionSummary,
  failWith,
  haltedControlState,
  healthResponse,
  portfolioResponse,
  providersResponse,
  proposalFixture,
  renderAt,
  stubFetch,
} from "../test/harness";

const readiness = {
  ready: true,
  database: "HEALTHY",
  schema_current: true,
  detail: null,
};

const emptyEvents = { total: 0, limit: 6, offset: 0, events: [] };

function routes(overrides: Record<string, object> = {}) {
  return {
    "GET /api/health": healthResponse,
    "GET /api/health/ready": readiness,
    "GET /api/health/providers": providersResponse,
    "GET /api/v1/execution/status": executionSummary,
    "GET /api/v1/system/execution-status": executionStatus,
    "GET /api/v1/system/control": controlState,
    "GET /api/v1/discovery/status": discoveryStatus,
    "GET /api/v1/proposals": emptyProposals,
    "GET /api/v1/research": [],
    "GET /api/v1/events": emptyEvents,
    "GET /api/v1/portfolio": portfolioResponse,
    ...overrides,
  };
}

describe("Dashboard", () => {
  it("puts the broker environment where it cannot be missed", async () => {
    stubFetch(routes());
    renderAt(<Dashboard />, "/");
    expect(await screen.findByText(/Broker: trading212 — DEMO/)).toBeInTheDocument();
  });

  it("surfaces a halt without requiring navigation", async () => {
    stubFetch(routes({ "GET /api/v1/system/control": haltedControlState }));
    renderAt(<Dashboard />, "/");

    expect(await screen.findByText("TRADING PAUSED")).toBeInTheDocument();
    expect(screen.getByText("trading is paused (budget review)")).toBeInTheDocument();
  });

  it("surfaces an unknown order state as the loudest thing on the page", async () => {
    stubFetch(
      routes({
        "GET /api/v1/execution/status": { ...executionSummary, ambiguous_attempts: 1 },
      }),
    );
    renderAt(<Dashboard />, "/");

    expect(await screen.findByText(/1 order in an unknown state/)).toBeInTheDocument();
    expect(screen.getByText("DO NOT RESEND")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Review the affected proposals/ })).toBeInTheDocument();
  });

  it("counts what is awaiting authorization and links to it", async () => {
    stubFetch(
      routes({
        "GET /api/v1/proposals": {
          ...emptyProposals,
          total: 1,
          items: [proposalFixture()],
          counts_by_status: { READY: 1 },
        },
      }),
    );
    renderAt(<Dashboard />, "/");

    const card = (await screen.findByText("Awaiting authorization")).closest(".card");
    expect(within(card as HTMLElement).getByText("1")).toBeInTheDocument();
    expect(within(card as HTMLElement).getByRole("link", { name: /Review proposals/ })).toBeInTheDocument();
    // And the proposal itself is listed, so the next click is the right one.
    expect(screen.getByText(/BUY 2 Apple Inc/)).toBeInTheDocument();
  });

  it("names the providers that need attention rather than only counting them", async () => {
    stubFetch(routes());
    renderAt(<Dashboard />, "/");

    const card = (await screen.findByText("Providers needing attention")).closest(".card");
    expect(within(card as HTMLElement).getByText("2")).toBeInTheDocument();
    expect(within(card as HTMLElement).getByText(/brave/)).toBeInTheDocument();
  });

  it("shows the account summary and says when the snapshot is stale", async () => {
    stubFetch(routes({ "GET /api/v1/portfolio": { ...portfolioResponse, stale: true } }));
    renderAt(<Dashboard />, "/");

    expect(await screen.findByText("1,000.00 GBP")).toBeInTheDocument();
    expect(screen.getByText(/snapshot is stale/)).toBeInTheDocument();
  });

  it("explains an empty event list rather than showing a blank panel", async () => {
    stubFetch(routes());
    renderAt(<Dashboard />, "/");
    expect(await screen.findByText("Nothing ingested yet")).toBeInTheDocument();
  });

  it("keeps working when one panel's request fails", async () => {
    // Per-panel polling is the point: a dead portfolio read must not blank the
    // execution posture next to it.
    stubFetch(routes({ "GET /api/v1/portfolio": failWith(503, "database unavailable") }));
    renderAt(<Dashboard />, "/");

    expect(await screen.findByText(/Broker: trading212 — DEMO/)).toBeInTheDocument();
    expect(screen.getByText("no snapshot")).toBeInTheDocument();
  });
});
