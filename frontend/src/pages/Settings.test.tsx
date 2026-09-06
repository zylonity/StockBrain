/**
 * The Settings page, and the promise it makes.
 *
 * The promise is that a value marked "restart required" has no switch. That is
 * the whole safety argument for having a settings page at all in front of a
 * system that can transmit real orders, so the tests here are mostly about
 * absence: no toggle on an execution gate, no secret in the DOM, no generic
 * write.
 */

import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Settings } from "./Settings";
import {
  controlState,
  discoveryStatus,
  failWith,
  haltedControlState,
  notificationPreferences,
  renderAt,
  settingsResponse,
  stubFetch,
} from "../test/harness";

function routes(overrides: Record<string, object> = {}) {
  return {
    "GET /api/v1/system/settings": settingsResponse,
    "GET /api/v1/system/control": controlState,
    "GET /api/v1/discovery/status": discoveryStatus,
    "GET /api/v1/system/telegram/preferences": notificationPreferences,
    ...overrides,
  };
}

describe("Settings: read model", () => {
  it("describes a setting with its effect and the variable that sets it", async () => {
    stubFetch(routes());
    renderAt(<Settings />, "/settings");

    await userEvent.click(await screen.findByText("Broker and execution gates"));
    expect(screen.getByText("T212_ENV")).toBeInTheDocument();
    expect(screen.getByText(/The single largest difference/)).toBeInTheDocument();
  });

  it("renders a secret as configured, never as a value", async () => {
    stubFetch(routes());
    renderAt(<Settings />, "/settings");

    await userEvent.click(await screen.findByText("Broker and execution gates"));
    const row = screen.getByText("Trading 212 API key").closest(".setting");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("configured")).toBeInTheDocument();
    // Nothing anywhere in the page may resemble a credential.
    expect(document.body.textContent).not.toContain("t212-secret");
  });

  it("gives an execution gate no control of any kind", async () => {
    stubFetch(routes());
    renderAt(<Settings />, "/settings");

    const group = (await screen.findByText("Broker and execution gates")).closest("details");
    expect(group).not.toBeNull();
    await userEvent.click(screen.getByText("Broker and execution gates"));

    const scope = within(group as HTMLElement);
    expect(scope.getAllByText("restart required").length).toBeGreaterThan(0);
    // No checkbox, no select, no button inside the gates group.
    expect(scope.queryByRole("checkbox")).toBeNull();
    expect(scope.queryByRole("combobox")).toBeNull();
    expect(scope.queryByRole("button")).toBeNull();
    expect(scope.getByText(/requires editing .env and restarting/)).toBeInTheDocument();
  });

  it("opens a group that has blockers, so an unusable subsystem is not hidden", async () => {
    stubFetch(routes());
    renderAt(<Settings />, "/settings");
    const group = (await screen.findByText("Broker and execution gates")).closest("details");
    expect(group).toHaveAttribute("open");
  });

  it("shows the server's reason when the catalogue cannot be read", async () => {
    stubFetch(routes({ "GET /api/v1/system/settings": failWith(503, "not configured") }));
    renderAt(<Settings />, "/settings");
    await waitFor(() =>
      expect(screen.getByText("Settings unavailable")).toBeInTheDocument(),
    );
  });
});

describe("Settings: runtime controls", () => {
  it("pauses trading through the server and re-reads the answer", async () => {
    const fetchStub = stubFetch(
      routes({ "POST /api/v1/system/pause": haltedControlState }),
    );
    renderAt(<Settings />, "/settings");

    const pause = await screen.findByRole("button", { name: "Pause" });
    await userEvent.click(pause);

    await waitFor(() => expect(fetchStub.callsTo("/api/v1/system/pause")).toHaveLength(1));
    // The reason is the entire body: no ticker, no quantity, no price.
    expect(fetchStub.callsTo("/api/v1/system/pause")[0]?.body).toEqual({
      reason: "paused from Settings",
    });
  });

  it("does not offer Resume as a way to release the kill switch", async () => {
    stubFetch(
      routes({
        "GET /api/v1/system/control": {
          ...haltedControlState,
          kill_switch: { ...haltedControlState.paused, flag: "control.kill_switch", active: true },
        },
      }),
    );
    renderAt(<Settings />, "/settings");

    // Both controls exist and are distinct: a routine resume must never undo an
    // emergency stop.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Release kill switch" })).toBeEnabled(),
    );
    expect(screen.getByText(/Resuming deliberately does not release the kill switch/)).toBeInTheDocument();
  });

  it("holds discovery separately from trading", async () => {
    const fetchStub = stubFetch(
      routes({ "POST /api/v1/discovery/pause": { paused: true, changed_at: null, actor: null, reason: null } }),
    );
    renderAt(<Settings />, "/settings");

    await userEvent.click(await screen.findByRole("button", { name: "Hold discovery" }));
    await waitFor(() => expect(fetchStub.callsTo("/api/v1/discovery/pause")).toHaveLength(1));
    // And it did not touch the trading pause.
    expect(fetchStub.callsTo("/api/v1/system/pause")).toHaveLength(0);
  });
});

describe("Settings: notification preferences", () => {
  it("saves only the categories that were changed", async () => {
    const fetchStub = stubFetch(
      routes({
        "PUT /api/v1/system/telegram/preferences": {
          ...notificationPreferences,
          updated_at: "2026-09-06T12:05:00Z",
          updated_by: "web:owner",
          categories: notificationPreferences.categories.map((category) =>
            category.category === "EVENT_DISCOVERED" ? { ...category, enabled: true } : category,
          ),
        },
      }),
    );
    renderAt(<Settings />, "/settings");

    const toggle = await screen.findByRole("checkbox", { name: /Article discovered/ });
    expect(toggle).not.toBeChecked();
    await userEvent.click(toggle);
    await userEvent.click(screen.getByRole("button", { name: "Save preferences" }));

    await waitFor(() =>
      expect(fetchStub.callsTo("/api/v1/system/telegram/preferences").filter((c) => c.method === "PUT")).toHaveLength(1),
    );
    const put = fetchStub
      .callsTo("/api/v1/system/telegram/preferences")
      .find((call) => call.method === "PUT");
    // A partial body: two tabs cannot silently revert each other's untouched
    // categories.
    expect(put?.body).toEqual({ categories: { EVENT_DISCOVERED: true } });
  });

  it("will not let the always-on category be switched off", async () => {
    stubFetch(routes());
    renderAt(<Settings />, "/settings");

    const locked = await screen.findByRole("checkbox", { name: /Order state unknown/ });
    expect(locked).toBeChecked();
    expect(locked).toBeDisabled();
    expect(screen.getByText("always on")).toBeInTheDocument();
  });

  it("keeps Save inert until something is actually different", async () => {
    stubFetch(routes());
    renderAt(<Settings />, "/settings");
    expect(await screen.findByRole("button", { name: "Save preferences" })).toBeDisabled();
  });

  it("reports why nothing can be delivered without hiding the switches", async () => {
    stubFetch(
      routes({
        "GET /api/v1/system/telegram/preferences": {
          ...notificationPreferences,
          delivery_available: false,
          blockers: ["TELEGRAM_ENABLED is false"],
        },
      }),
    );
    renderAt(<Settings />, "/settings");

    expect(await screen.findByText("Nothing can be delivered right now")).toBeInTheDocument();
    expect(screen.getByText("TELEGRAM_ENABLED is false")).toBeInTheDocument();
    // Still editable: the preference is saved and applies when delivery works.
    expect(screen.getByRole("checkbox", { name: /Article discovered/ })).toBeEnabled();
  });

  it("shows the server's refusal rather than pretending the save worked", async () => {
    stubFetch(
      routes({
        "PUT /api/v1/system/telegram/preferences": failWith(403, "a valid CSRF header is required"),
      }),
    );
    renderAt(<Settings />, "/settings");

    await userEvent.click(await screen.findByRole("checkbox", { name: /Article discovered/ }));
    await userEvent.click(screen.getByRole("button", { name: "Save preferences" }));

    await waitFor(() =>
      expect(screen.getByText("a valid CSRF header is required")).toBeInTheDocument(),
    );
  });
});
