/**
 * Proposal controls: the one place in this interface where a click has money
 * behind it.
 *
 * Three things are load-bearing and each has a test:
 *
 * 1. **Two steps to authorize.** The first click opens a confirmation; only the
 *    second reaches the server. Rejecting is also confirmed, because it is
 *    durable and terminal.
 * 2. **The request names nothing.** Every order parameter is read from the
 *    proposal row on the server under lock, so the body carries at most a
 *    reason.
 * 3. **The page does not assert what the broker did.** Whether an order exists
 *    is read from the proposal, never printed unconditionally -- the previous
 *    version said "No broker order has been sent" on every proposal in every
 *    state, which stopped being true when transmission shipped.
 */

import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { render } from "@testing-library/react";

import { ProposalDetail, Proposals } from "./Proposals";
import {
  emptyProposals,
  executionPolicy,
  failWith,
  proposalFixture,
  renderAt,
  stubFetch,
} from "../test/harness";

const PROPOSAL_ID = "11111111-1111-1111-1111-111111111111";

const emptyExecution = {
  proposal_id: PROPOSAL_ID,
  proposal_status: "READY",
  broker_environment: "demo",
  authorization_source: null,
  execution_policy: "MANUAL",
  transmitted: false,
  ambiguous: false,
  reconciliation_required: false,
  attempts: [],
  orders: [],
  notice: "Nothing has been transmitted.",
};

const emptyRisk = {
  proposal_id: PROPOSAL_ID,
  risk_outcome: "ALLOW",
  risk_policy_version: "7af326e1",
  risk_snapshot_hash: null,
  rules: [],
  blockers: [],
  warnings: [],
  reductions: [],
  sizing_reasons: [],
  snapshot: {},
  evaluations: [],
};

function detailRoutes(proposal: object, overrides: Record<string, object> = {}) {
  return {
    [`GET /api/v1/proposals/${PROPOSAL_ID}`]: proposal,
    [`GET /api/v1/proposals/${PROPOSAL_ID}/risk`]: emptyRisk,
    [`GET /api/v1/proposals/${PROPOSAL_ID}/execution`]: emptyExecution,
    ...overrides,
  };
}

function renderDetail(routes: Record<string, object>) {
  stubFetch(routes);
  return render(
    <MemoryRouter initialEntries={[`/proposals/${PROPOSAL_ID}`]}>
      <Routes>
        <Route path="/proposals/:proposalId" element={<ProposalDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Proposals list", () => {
  it("explains what an empty list means and where to look next", async () => {
    stubFetch({
      "GET /api/v1/proposals": emptyProposals,
      "GET /api/v1/proposals/policy": executionPolicy,
    });
    renderAt(<Proposals />, "/proposals");

    expect(await screen.findByText("No proposals yet")).toBeInTheDocument();
    expect(screen.getByText(/deterministic risk engine allows it/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Research runs" })).toBeInTheDocument();
  });

  it("does not claim an order was never sent", async () => {
    stubFetch({
      "GET /api/v1/proposals": emptyProposals,
      "GET /api/v1/proposals/policy": executionPolicy,
    });
    renderAt(<Proposals />, "/proposals");
    await screen.findByText("No proposals yet");
    // The subtitle describes the gating, and asserts nothing about the broker.
    expect(document.body.textContent).not.toContain("no order-submission path");
    expect(screen.getByText(/Transmission is a separate, separately gated step/)).toBeInTheDocument();
  });

  it("shows the server's own reason when the list cannot be read", async () => {
    stubFetch({
      "GET /api/v1/proposals": failWith(503, "the proposal service is not configured"),
      "GET /api/v1/proposals/policy": executionPolicy,
    });
    renderAt(<Proposals />, "/proposals");
    expect(
      await screen.findByText("the proposal service is not configured"),
    ).toBeInTheDocument();
  });
});

describe("Proposal authorization", () => {
  it("requires a second, explicit confirmation before it reaches the server", async () => {
    const fetchStub = stubFetch(detailRoutes(proposalFixture()));
    render(
      <MemoryRouter initialEntries={[`/proposals/${PROPOSAL_ID}`]}>
        <Routes>
          <Route path="/proposals/:proposalId" element={<ProposalDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    // Nothing has been sent yet: the first click only opens the confirmation.
    expect(fetchStub.callsTo(`/api/v1/proposals/${PROPOSAL_ID}/approve`)).toHaveLength(0);
    expect(screen.getByText(/re-checked before this is recorded/)).toBeInTheDocument();

    fetchStub.set(
      `POST /api/v1/proposals/${PROPOSAL_ID}/approve`,
      proposalFixture({ status: "APPROVED", can_approve: false }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Confirm authorization" }));

    await waitFor(() =>
      expect(fetchStub.callsTo(`/api/v1/proposals/${PROPOSAL_ID}/approve`)).toHaveLength(1),
    );
    // The body names nothing the server would have to trust.
    expect(fetchStub.callsTo(`/api/v1/proposals/${PROPOSAL_ID}/approve`)[0]?.body).toEqual({});
  });

  it("can be backed out of without sending anything", async () => {
    const fetchStub = stubFetch(detailRoutes(proposalFixture()));
    renderDetail(detailRoutes(proposalFixture()));

    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(fetchStub.callsTo(`/api/v1/proposals/${PROPOSAL_ID}/approve`)).toHaveLength(0);
  });

  it("shows a risk refusal as an answer, with its status", async () => {
    // A 422 from the risk engine is a verdict, not a transport failure: it is
    // shown verbatim and never retried.
    const fetchStub = stubFetch(
      detailRoutes(proposalFixture(), {
        [`POST /api/v1/proposals/${PROPOSAL_ID}/approve`]: failWith(
          422,
          "deterministic risk refused: cash reserve",
        ),
      }),
    );
    render(
      <MemoryRouter initialEntries={[`/proposals/${PROPOSAL_ID}`]}>
        <Routes>
          <Route path="/proposals/:proposalId" element={<ProposalDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm authorization" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("422");
    expect(alert).toHaveTextContent("deterministic risk refused: cash reserve");
    // One attempt. A refusal is not something to try again.
    expect(fetchStub.callsTo(`/api/v1/proposals/${PROPOSAL_ID}/approve`)).toHaveLength(1);
  });

  it("offers no action on a proposal the server says cannot be acted on", async () => {
    renderDetail(
      detailRoutes(
        proposalFixture({
          status: "EXPIRED",
          can_approve: false,
          can_reject: false,
          can_cancel: false,
        }),
      ),
    );

    await screen.findByText("EXPIRED");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
  });
});

describe("Proposal execution state", () => {
  it("says no order has been transmitted when none has", async () => {
    renderDetail(detailRoutes(proposalFixture()));
    expect(await screen.findByText("No order has been transmitted")).toBeInTheDocument();
  });

  it("says an order has been transmitted when one has", async () => {
    renderDetail(
      detailRoutes(
        proposalFixture({
          status: "EXECUTED",
          broker_order_transmitted: true,
          can_approve: false,
          can_reject: false,
          can_cancel: false,
        }),
      ),
    );
    expect(
      await screen.findByText("An order has been transmitted for this proposal"),
    ).toBeInTheDocument();
    expect(screen.queryByText("No order has been transmitted")).toBeNull();
  });

  it("warns unmissably and offers no resend when the outcome is unknown", async () => {
    renderDetail(
      detailRoutes(proposalFixture({ status: "EXECUTION_AMBIGUOUS", broker_order_transmitted: true }), {
        [`GET /api/v1/proposals/${PROPOSAL_ID}/execution`]: {
          ...emptyExecution,
          proposal_status: "EXECUTION_AMBIGUOUS",
          transmitted: true,
          ambiguous: true,
          reconciliation_required: true,
          notice: "The order may or may not exist.",
          attempts: [
            {
              id: "att-1",
              proposal_id: PROPOSAL_ID,
              attempt_number: 1,
              broker_environment: "demo",
              outcome: "AMBIGUOUS",
              ambiguous: true,
              sent_to_broker: true,
              sent_at: "2026-09-06T12:00:00Z",
              started_at: "2026-09-06T11:59:59Z",
              preflight_at: null,
              completed_at: null,
              http_status: 408,
              broker_order_id: null,
              request_fingerprint: "abc",
              error: null,
              error_category: "BROKER_TIMEOUT",
              reconciled_at: null,
              reconciliation_result: null,
              reconciliation_attempts: 0,
              reconciliation_detail: {},
              rate_limit: {},
              execution_snapshot: {},
              resend_permitted: false,
            },
          ],
        },
      }),
    );

    expect(
      await screen.findByText(/ORDER STATE UNKNOWN — DO NOT RESEND/),
    ).toBeInTheDocument();
    // Reconciliation is a read of the broker and is the only offered action.
    expect(screen.getByRole("button", { name: "Reconcile" })).toBeInTheDocument();
    for (const forbidden of [/resend/i, /retry/i, /submit order/i]) {
      expect(screen.queryByRole("button", { name: forbidden })).toBeNull();
    }
  });
});
