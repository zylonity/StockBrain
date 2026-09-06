/**
 * Display formatting for values that came from a financial decision.
 *
 * The rule these tests enforce is that nothing here changes a number. Decimals
 * arrive as strings because that is what the server persisted and what the risk
 * engine sized against; grouping and padding are presentation, and a value that
 * displays differently from the one the decision was made on is a value nobody
 * can reconcile afterwards.
 */

import { describe, expect, it } from "vitest";

import {
  formatDecimal,
  formatDuration,
  formatMoney,
  formatPercent,
  formatQuantity,
  formatRelative,
  formatTimestamp,
  formatUsd,
  hostOf,
  isNegative,
} from "./formats";

describe("formatDecimal", () => {
  it("groups the integer part without touching the digits", () => {
    expect(formatDecimal("14238.7100")).toBe("14,238.7100");
    expect(formatDecimal("1000000")).toBe("1,000,000");
  });

  it("pads or truncates to a requested precision without rounding up", () => {
    expect(formatDecimal("178.42", { places: 4 })).toBe("178.4200");
    // Truncated, never rounded: 178.4299 must not become 178.43 in a column an
    // operator reconciles against a broker statement.
    expect(formatDecimal("178.4299", { places: 2 })).toBe("178.42");
  });

  it("keeps a negative sign", () => {
    expect(formatDecimal("-183.26")).toBe("-183.26");
    expect(isNegative("-183.26")).toBe(true);
    expect(isNegative("183.26")).toBe(false);
  });

  it("returns a dash for absent values rather than a zero", () => {
    // A missing spread and a zero spread mean opposite things.
    expect(formatDecimal(null)).toBe("—");
    expect(formatDecimal(undefined)).toBe("—");
    expect(formatDecimal("")).toBe("—");
    expect(formatDecimal("0")).toBe("0");
  });

  it("passes through anything it does not recognise, rather than mangling it", () => {
    expect(formatDecimal("1e9")).toBe("1e9");
  });
});

describe("formatQuantity", () => {
  it("removes storage padding without removing precision", () => {
    // Numeric(28, 10) arrives fully padded; ten digits of precision nobody has
    // is not information.
    expect(formatQuantity("2.0000000000")).toBe("2");
    expect(formatQuantity("48.0000000000")).toBe("48");
    expect(formatQuantity("0.5000000000")).toBe("0.5");
    // The last significant digit survives; nothing is rounded away.
    expect(formatQuantity("2.0000000001")).toBe("2.0000000001");
  });

  it("groups large quantities", () => {
    expect(formatQuantity("12000.0000000000")).toBe("12,000");
  });

  it("is a dash when there is no quantity", () => {
    expect(formatQuantity(null)).toBe("—");
  });
});

describe("formatMoney", () => {
  it("carries the currency the server reported", () => {
    expect(formatMoney("284.6000", "GBP")).toBe("284.60 GBP");
    expect(formatMoney("284.6000", null)).toBe("284.60");
  });

  it("never invents a currency for a missing amount", () => {
    expect(formatMoney(null, "GBP")).toBe("—");
  });
});

describe("formatPercent", () => {
  it("renders a model confidence as a percentage", () => {
    expect(formatPercent(0.78)).toBe("78%");
    expect(formatPercent(0.7812, 1)).toBe("78.1%");
  });

  it("is a dash for an absent confidence, not 0%", () => {
    // "0% confidence" and "no thesis" are different facts.
    expect(formatPercent(null)).toBe("—");
  });
});

describe("times", () => {
  it("renders a timestamp as an unambiguous UTC instant", () => {
    expect(formatTimestamp("2026-09-06T12:00:00Z")).toBe("2026-09-06 12:00:00Z");
  });

  it("survives a malformed timestamp instead of rendering Invalid Date", () => {
    expect(formatTimestamp("not-a-date")).toBe("—");
    expect(formatRelative("not-a-date")).toBe("—");
  });

  it("says when something is still in the future", () => {
    const soon = new Date(Date.now() + 20 * 60_000).toISOString();
    expect(formatRelative(soon)).toMatch(/^in \d+m$/);
  });

  it("scales a duration to the unit an operator reads", () => {
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(300)).toBe("5m");
    expect(formatDuration(7200)).toBe("2h");
    expect(formatDuration(null)).toBe("—");
  });
});

describe("misc", () => {
  it("formats a USD cost from either a string or a number", () => {
    expect(formatUsd("0.004312")).toBe("$0.0043");
    expect(formatUsd(0.5, 2)).toBe("$0.50");
    expect(formatUsd(null)).toBe("—");
  });

  it("reduces a URL to a hostname for compact attribution", () => {
    expect(hostOf("https://www.reuters.com/article/x")).toBe("reuters.com");
    expect(hostOf("not a url")).toBe("not a url");
    expect(hostOf(null)).toBe("—");
  });
});
