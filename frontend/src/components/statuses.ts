import type { ProviderStatus } from "../api/types";

/**
 * What each health state actually means for the operator.
 *
 * The words alone are not enough. DISABLED and DOWN both read as "not working"
 * at a glance, and only one of them is a problem; BUDGET_EXHAUSTED reads as a
 * fault and is a spending limit doing its job. The status is always rendered
 * with its meaning available -- as a tooltip beside a pill, and in full in the
 * legend on the health page.
 */
export const STATUS_MEANING: Record<ProviderStatus, string> = {
  HEALTHY: "Reached and working at the last check.",
  BUDGET_EXHAUSTED:
    "Working, and out of allowance. A spending limit doing its job, not a fault: it clears when the budget window rolls over.",
  DEGRADED:
    "Partly working. Some calls are failing, or a credential is valid but the plan does not cover the feature.",
  DOWN: "Failing every call. Only this provider's subsystem is affected; the rest of the application keeps running.",
  DISABLED:
    "Deliberately switched off, or not configured. Never a fault, and never counted as one.",
  UNKNOWN: "Configured but not checked yet. It fills in once the first probe or call completes.",
};

/** The one-word summary of what to do about a status. */
export const STATUS_ACTION: Record<ProviderStatus, string> = {
  HEALTHY: "Nothing to do.",
  BUDGET_EXHAUSTED: "Raise the cap in Settings, or wait for the window to roll over.",
  DEGRADED: "Check this provider's logs for the failing calls.",
  DOWN: "Check this provider's logs and its credential.",
  DISABLED: "Configure it in Settings if you want it running.",
  UNKNOWN: "Wait for the first check, or refresh.",
};
