/**
 * The login gate.
 *
 * It is explicitly *not* the security boundary -- the server refuses every
 * protected route on its own -- so what it must get right is the honesty of
 * what it tells the operator. Three cases have three different remedies and
 * must never look alike: a wrong password, a lapsed session, and
 * "authentication is switched on but unusable", which answers 503 and needs a
 * password hash on the host.
 */

import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AuthGate } from "./AuthGate";
import { failWith, renderAt, stubFetch } from "../test/harness";

const AUTHENTICATED = {
  authenticated: true,
  auth_required: true,
  username: "owner",
  expires_at: "2026-09-07T00:00:00Z",
  blockers: [],
};

const ANONYMOUS = {
  authenticated: false,
  auth_required: true,
  username: null,
  expires_at: null,
  blockers: [],
};

const TRUSTED_NETWORK = {
  authenticated: false,
  auth_required: false,
  username: null,
  expires_at: null,
  blockers: [],
};

const UNUSABLE = {
  authenticated: false,
  auth_required: true,
  username: null,
  expires_at: null,
  blockers: ["WEB_OWNER_PASSWORD_HASH is not set"],
};

describe("AuthGate", () => {
  it("renders the application for an authenticated browser", async () => {
    stubFetch({ "GET /api/v1/auth/session": AUTHENTICATED });
    renderAt(
      <AuthGate>
        <p>protected content</p>
      </AuthGate>,
    );
    expect(await screen.findByText("protected content")).toBeInTheDocument();
  });

  it("renders the application when the deployment relies on network trust", async () => {
    // A supported configuration. The gate must not demand a password that no
    // longer exists to check.
    stubFetch({ "GET /api/v1/auth/session": TRUSTED_NETWORK });
    renderAt(
      <AuthGate>
        <p>protected content</p>
      </AuthGate>,
    );
    expect(await screen.findByText("protected content")).toBeInTheDocument();
  });

  it("shows a login form instead of a page full of 401s", async () => {
    stubFetch({ "GET /api/v1/auth/session": ANONYMOUS });
    renderAt(
      <AuthGate>
        <p>protected content</p>
      </AuthGate>,
    );
    expect(await screen.findByLabelText("Username")).toBeInTheDocument();
    expect(screen.queryByText("protected content")).toBeNull();
  });

  it("signs in and reveals the application", async () => {
    const fetchStub = stubFetch({
      "GET /api/v1/auth/session": ANONYMOUS,
      "POST /api/v1/auth/login": AUTHENTICATED,
    });
    renderAt(
      <AuthGate>
        <p>protected content</p>
      </AuthGate>,
    );

    await userEvent.type(await screen.findByLabelText("Username"), "owner");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse");
    fetchStub.set("GET /api/v1/auth/session", AUTHENTICATED);
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("protected content")).toBeInTheDocument();
    const login = fetchStub.callsTo("/api/v1/auth/login")[0];
    expect(login?.body).toEqual({ username: "owner", password: "correct-horse" });
    // The login is a state change and carries the CSRF header like any other.
    expect(login?.headers).toHaveProperty("X-StockBrain-CSRF");
  });

  it("reports a wrong password as a wrong password", async () => {
    stubFetch({
      "GET /api/v1/auth/session": ANONYMOUS,
      "POST /api/v1/auth/login": failWith(401, "invalid credentials"),
    });
    renderAt(
      <AuthGate>
        <p>protected content</p>
      </AuthGate>,
    );

    await userEvent.type(await screen.findByLabelText("Username"), "owner");
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(
      await screen.findByText("Incorrect username or password."),
    ).toBeInTheDocument();
  });

  it("shows a misconfiguration verbatim, because the remedy is on the host", async () => {
    // The useful case: authentication is on and unusable, every route answers
    // 503, and the message names the missing variable.
    stubFetch({
      "GET /api/v1/auth/session": UNUSABLE,
      "POST /api/v1/auth/login": failWith(503, "web authentication is enabled but not configured"),
    });
    renderAt(
      <AuthGate>
        <p>protected content</p>
      </AuthGate>,
    );

    expect(await screen.findByText("WEB_OWNER_PASSWORD_HASH is not set")).toBeInTheDocument();
    // And signing in is refused rather than offered as a way out.
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("falls back to the login form when the session request itself fails", async () => {
    stubFetch({ "GET /api/v1/auth/session": failWith(500, "database unavailable") });
    renderAt(
      <AuthGate>
        <p>protected content</p>
      </AuthGate>,
    );

    await waitFor(() => expect(screen.getByLabelText("Username")).toBeInTheDocument());
    expect(screen.getByText("database unavailable")).toBeInTheDocument();
    expect(screen.queryByText("protected content")).toBeNull();
  });
});
