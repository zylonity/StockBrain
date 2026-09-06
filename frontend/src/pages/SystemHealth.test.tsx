/**
 * System health.
 *
 * The behaviour worth testing is not that statuses render -- it is that they
 * are *distinguishable*. DISABLED and DOWN look equally alarming as bare words
 * and only one is a problem; BUDGET_EXHAUSTED reads as a fault and is a
 * spending limit working correctly. So every state carries its meaning and its
 * next step, and the page groups providers by subsystem rather than listing
 * them alphabetically as if a dead news feed and a dead broker were comparable.
 */

import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { SystemHealth } from "./SystemHealth";
import {
  controlState,
  discoveryStatus,
  executionSummary,
  failWith,
  fxStatus,
  haltedControlState,
  healthResponse,
  logFacets,
  logsResponse,
  providersResponse,
  renderAt,
  stubFetch,
  telegramStatus,
  webSecurity,
} from "../test/harness";

function routes(overrides: Record<string, object> = {}) {
  return {
    "GET /api/health/providers": providersResponse,
    "GET /api/health": healthResponse,
    "GET /api/v1/system/control": controlState,
    "GET /api/v1/system/telegram": telegramStatus,
    "GET /api/v1/execution/status": executionSummary,
    "GET /api/v1/discovery/status": discoveryStatus,
    "GET /api/v1/system/fx": fxStatus,
    "GET /api/v1/system/web-security": webSecurity,
    "GET /api/v1/system/logs": logsResponse,
    "GET /api/v1/system/logs/facets": logFacets,
    ...overrides,
  };
}

describe("SystemHealth", () => {
  it("groups providers by subsystem", async () => {
    stubFetch(routes());
    renderAt(<SystemHealth />, "/health");

    // The subsystem headings, each with the note that says why its providers
    // are grouped together at all.
    expect(await screen.findByText("Database")).toBeInTheDocument();
    expect(screen.getByText(/only load-bearing dependency/)).toBeInTheDocument();
    expect(screen.getByText(/Redundant by design/)).toBeInTheDocument();
    expect(screen.getByText(/Research and market data/)).toBeInTheDocument();
  });

  it("gives every state a meaning and a next step, not just a label", async () => {
    stubFetch(routes());
    renderAt(<SystemHealth />, "/health");

    const row = (await screen.findByText("brave")).closest("tr");
    expect(within(row as HTMLElement).getByText("DOWN")).toBeInTheDocument();
    expect(
      within(row as HTMLElement).getByText(/Check this provider's logs and its credential/),
    ).toBeInTheDocument();

    // And the legend spells out all six, because a tooltip is invisible on a
    // phone, which is where an alert gets read.
    expect(
      screen.getByText(/A spending limit doing its job, not a fault/),
    ).toBeInTheDocument();
  });

  it("distinguishes an exhausted budget from a fault", async () => {
    stubFetch(routes());
    renderAt(<SystemHealth />, "/health");

    const row = (await screen.findByText("exa")).closest("tr");
    expect(within(row as HTMLElement).getByText("BUDGET_EXHAUSTED")).toBeInTheDocument();
    expect(
      within(row as HTMLElement).getByText(/Raise the cap in Settings/),
    ).toBeInTheDocument();
    // Not counted as a failure.
    expect(within(row as HTMLElement).getByText("0")).toBeInTheDocument();
  });

  it("counts what needs attention and names it", async () => {
    stubFetch(routes());
    renderAt(<SystemHealth />, "/health");

    const card = (await screen.findByText("Needing attention")).closest(".card");
    expect(within(card as HTMLElement).getByText("2")).toBeInTheDocument();
    expect(within(card as HTMLElement).getByText(/brave/)).toBeInTheDocument();
  });

  it("makes a halt unmissable and points at how to lift it", async () => {
    stubFetch(routes({ "GET /api/v1/system/control": haltedControlState }));
    renderAt(<SystemHealth />, "/health");

    expect(await screen.findByText("TRADING PAUSED")).toBeInTheDocument();
    expect(screen.getByText("trading is paused (budget review)")).toBeInTheDocument();
    // And Resume becomes the enabled control while Pause does not.
    await waitFor(() => expect(screen.getByRole("button", { name: "Resume" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Pause" })).toBeDisabled();
  });

  it("sends a control change to the server and re-reads the answer", async () => {
    const fetchStub = stubFetch(
      routes({ "POST /api/v1/system/kill-switch": haltedControlState }),
    );
    renderAt(<SystemHealth />, "/health");

    await userEvent.click(await screen.findByRole("button", { name: "Engage kill switch" }));

    await waitFor(() =>
      expect(fetchStub.callsTo("/api/v1/system/kill-switch")).toHaveLength(1),
    );
    const body = fetchStub.callsTo("/api/v1/system/kill-switch")[0]?.body as Record<string, unknown>;
    // A boolean and a reason. Nothing that could name a ticker or a size.
    expect(Object.keys(body).sort()).toEqual(["engaged", "reason"]);
    expect(body.engaged).toBe(true);
  });

  it("shows a control failure without pretending it succeeded", async () => {
    stubFetch(
      routes({
        "POST /api/v1/system/pause": failWith(403, "cross-origin request refused"),
      }),
    );
    renderAt(<SystemHealth />, "/health");

    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    await waitFor(() =>
      expect(screen.getByText("cross-origin request refused")).toBeInTheDocument(),
    );
  });

  it("warns loudly when the interface is not password-protected", async () => {
    stubFetch(
      routes({
        "GET /api/v1/system/web-security": {
          ...webSecurity,
          auth_effective: false,
          blockers: ["WEB_OWNER_PASSWORD_HASH is not set"],
        },
      }),
    );
    renderAt(<SystemHealth />, "/health");

    expect(
      await screen.findByText("This interface is not password-protected"),
    ).toBeInTheDocument();
  });

  it("keeps the rest of the page usable when one panel fails", async () => {
    // A dead FX probe must not take the health board down with it: the whole
    // point of per-provider health is that one failure is local.
    stubFetch(routes({ "GET /api/v1/system/fx": failWith(502, "frankfurter unreachable") }));
    renderAt(<SystemHealth />, "/health");

    expect(await screen.findByText("frankfurter unreachable")).toBeInTheDocument();
    expect(screen.getByText("Database")).toBeInTheDocument();
    expect(screen.getByText("brave")).toBeInTheDocument();
  });

  it("shows the server's reason when provider health itself cannot be read", async () => {
    stubFetch(routes({ "GET /api/health/providers": failWith(503, "database unavailable") }));
    renderAt(<SystemHealth />, "/health");

    await waitFor(() =>
      expect(screen.getByText("Provider health unavailable")).toBeInTheDocument(),
    );
  });
});
