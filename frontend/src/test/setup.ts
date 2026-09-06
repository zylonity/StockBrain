import "@testing-library/jest-dom/vitest";

import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  document.cookie = "sb_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
  vi.useRealTimers();
});

/**
 * `usePolling` reads `document.visibilityState` and installs an interval.
 *
 * jsdom reports "visible" by default, which is what we want -- the polling path
 * should be the one under test -- but the property is not writable, so a test
 * that wants a hidden tab has to define it.
 */
Object.defineProperty(document, "visibilityState", {
  configurable: true,
  get: () => "visible",
});
