/**
 * The Events page's provider filter.
 *
 * This list has drifted from the server twice: BRAVE and EXA were missing after
 * the discovery provider split, and the five disclosure feeds repeated it. The
 * failure is invisible from the server side -- the API accepted those filters
 * the whole time -- and shows up only as a source row a user can see but cannot
 * filter on. So the test checks the dropdown against the server's own
 * `SourceProvider` values rather than against a count, and then proves the
 * choice actually reaches the request.
 */

import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Events } from "./Events";
import { renderAt, stubFetch } from "../test/harness";

/** Shaped like `EventListResponse`; the filter is independent of the rows. */
const EMPTY_PAGE = { total: 0, limit: 20, offset: 0, events: [] };

/** Mirrors `backend/stockbrain/enums.py::SourceProvider`, in order. */
const SERVER_PROVIDERS = [
  "ALPACA",
  "BRAVE",
  "EXA",
  "FIRECRAWL",
  "SEC",
  "MANUAL",
  "INVESTEGATE",
  "EQS",
  "CNMV",
  "GLOBENEWSWIRE",
  "ACTUSNEWS",
];

describe("Events provider filter", () => {
  it("offers every provider the server can emit", async () => {
    stubFetch({ "GET /api/v1/events": EMPTY_PAGE });
    renderAt(<Events />, "/events");

    const select = await screen.findByLabelText("Filter by source provider");
    const values = within(select)
      .getAllByRole("option")
      .map((option) => option.getAttribute("value") ?? "");

    expect(values).toEqual(expect.arrayContaining(SERVER_PROVIDERS));
  });

  it("sends a disclosure feed to the server as the provider filter", async () => {
    const fetchStub = stubFetch({ "GET /api/v1/events": EMPTY_PAGE });
    renderAt(<Events />, "/events");

    const select = await screen.findByLabelText("Filter by source provider");
    await userEvent.selectOptions(select, "GLOBENEWSWIRE");

    await waitFor(() => {
      const urls = fetchStub.callsTo("/api/v1/events").map((call) => call.url);
      expect(urls.some((url) => url.includes("provider=GLOBENEWSWIRE"))).toBe(true);
    });
  });
});
