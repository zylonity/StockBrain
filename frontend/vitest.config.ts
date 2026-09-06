import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

/**
 * Component tests run in jsdom against a stubbed `fetch`.
 *
 * Deliberately no MSW and no test server: every network call in this
 * application goes through one thin wrapper (`src/api/client.ts`), so stubbing
 * `fetch` exercises the *real* client -- its CSRF header, its error unwrapping,
 * its query-string building -- rather than a mock of it. A request interceptor
 * would test the interceptor.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
