/**
 * The API client is the whole security surface of the frontend.
 *
 * Everything protective about this browser's requests lives in one file: the
 * same-origin credential policy, the double-submit CSRF header, and the refusal
 * to put order parameters in a request body. So these are the tests that would
 * catch a change to any of it -- and they run against the real client rather
 * than a mock, because a mocked client proves nothing about a header it does
 * not send.
 */

import { beforeEach, describe, expect, it } from "vitest";

import { ApiError, api, csrfToken, logQuery } from "./client";
import { failWith, stubFetch } from "../test/harness";

beforeEach(() => {
  document.cookie = "sb_csrf=csrf-token-value";
});

describe("CSRF", () => {
  it("reads the token from the readable half of the double-submit pair", () => {
    expect(csrfToken()).toBe("csrf-token-value");
  });

  it("returns an empty token rather than throwing when the cookie is absent", () => {
    document.cookie = "sb_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
    // The server refuses the request; the client must not crash before it can.
    expect(csrfToken()).toBe("");
  });

  it("sends the token on every state-changing call", async () => {
    const fetchStub = stubFetch({
      "POST /api/v1/system/pause": {},
      "PUT /api/v1/system/telegram/preferences": {},
      "POST /api/v1/proposals/abc/approve": {},
    });

    await api.pauseTrading("test");
    await api.updateNotificationPreferences({ EVENT_DISCOVERED: true });
    await api.approveProposal("abc");

    expect(fetchStub.calls).toHaveLength(3);
    for (const call of fetchStub.calls) {
      expect(call.headers["X-StockBrain-CSRF"]).toBe("csrf-token-value");
    }
  });

  it("re-reads the cookie per call, so a session re-established elsewhere keeps working", async () => {
    const fetchStub = stubFetch({ "POST /api/v1/system/pause": {} });
    await api.pauseTrading();
    document.cookie = "sb_csrf=a-newer-token";
    await api.pauseTrading();

    expect(fetchStub.calls[0]?.headers["X-StockBrain-CSRF"]).toBe("csrf-token-value");
    expect(fetchStub.calls[1]?.headers["X-StockBrain-CSRF"]).toBe("a-newer-token");
  });

  it("does not send a CSRF header on a read", async () => {
    const fetchStub = stubFetch({ "GET /api/v1/system/control": {} });
    await api.controlState();
    expect(fetchStub.calls[0]?.headers["X-StockBrain-CSRF"]).toBeUndefined();
  });
});

describe("session credentials", () => {
  it("sends the cookie same-origin and never cross-origin", async () => {
    stubFetch({ "GET /api/v1/auth/session": {} });
    await api.session();
    const call = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    expect((call?.[1] as RequestInit).credentials).toBe("same-origin");
  });
});

describe("order parameters", () => {
  it("never puts a ticker, quantity or price in a request body", async () => {
    const fetchStub = stubFetch({
      "POST /api/v1/proposals/abc/approve": {},
      "POST /api/v1/proposals/abc/reject": {},
      "POST /api/v1/execution/attempts/att-1/reconcile": {},
    });

    await api.approveProposal("abc");
    await api.rejectProposal("abc", "not now");
    await api.reconcileAttempt("att-1");

    // The server reads every order parameter from the proposal row under lock.
    // A client that *could* name a quantity would be a client that could size a
    // trade, so the bodies carry a free-text reason at most.
    for (const call of fetchStub.calls) {
      const body = JSON.stringify(call.body ?? {});
      for (const forbidden of ["ticker", "quantity", "price", "side", "account", "broker"]) {
        expect(body).not.toContain(forbidden);
      }
    }
    expect(fetchStub.calls[1]?.body).toEqual({ reason: "not now" });
  });

  it("offers no resend of an order anywhere in the client", async () => {
    // Trading 212's order POST is non-idempotent: a resend would create a
    // second position. Reconciliation is a *read* of the broker and is the only
    // mutating execution call the client knows how to make.
    const surface = Object.keys(api).join(" ").toLowerCase();
    for (const forbidden of ["resend", "retry", "resubmit", "placeorder", "submitorder"]) {
      expect(surface).not.toContain(forbidden);
    }
  });
});

describe("errors", () => {
  it("unwraps the server's own detail rather than the status line", async () => {
    stubFetch({
      "GET /api/v1/system/settings": failWith(503, "web authentication is not configured"),
    });
    await expect(api.settings()).rejects.toThrow("web authentication is not configured");
  });

  it("carries the status, so a 401 and a 503 can be told apart", async () => {
    stubFetch({ "GET /api/v1/auth/session": failWith(401, "authentication required") });
    const error = await api.session().catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(401);
  });

  it("reports a network failure as status 0 rather than pretending it was HTTP", async () => {
    // A page that cannot distinguish "the server said no" from "there is no
    // server" shows the operator the wrong remedy.
    stubFetch({});
    const { vi } = await import("vitest");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const error = await api.health().catch((cause: unknown) => cause);
    expect((error as ApiError).status).toBe(0);
    expect((error as ApiError).message).toContain("Network error");
  });

  it("falls back to the status line when the error body is not JSON", async () => {
    const { vi } = await import("vitest");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("<html>gateway</html>", { status: 502 })),
    );
    await expect(api.health()).rejects.toThrow("502");
  });
});

describe("log query building", () => {
  it("emits only narrowing parameters, and no filesystem parameter", () => {
    const query = logQuery({
      minLevel: "warning",
      services: ["brave", "exa"],
      categories: ["discovery"],
      sinceMinutes: 60,
      search: "  rate limited  ",
      limit: 50,
      offset: 100,
    });
    const params = new URLSearchParams(query);
    expect(params.get("min_level")).toBe("warning");
    expect(params.get("services")).toBe("brave,exa");
    expect(params.get("categories")).toBe("discovery");
    expect(params.get("since_minutes")).toBe("60");
    expect(params.get("search")).toBe("rate limited");
    expect(params.get("limit")).toBe("50");
    expect(params.get("offset")).toBe("100");
    // Nothing that could name a file, a container or a command.
    expect([...params.keys()].sort()).toEqual([
      "categories",
      "limit",
      "min_level",
      "offset",
      "search",
      "services",
      "since_minutes",
    ]);
  });

  it("omits empty filters instead of sending blanks", () => {
    const params = new URLSearchParams(logQuery({ search: "   " }));
    expect(params.has("search")).toBe(false);
    expect(params.get("limit")).toBe("100");
  });
});
