/**
 * Plan chosen on the marketing site (`/login?mode=register&plan=growth_5000`). The query param only survives the
 * first page, so it is parked in localStorage until the tenant buys a plan (cleared after a successful checkout).
 * Only a preselection: nothing is charged without the owner pressing the plan's button.
 */
const KEY = "pending_plan";
/** Same shape as billing_plans.code ("growth_5000"); anything else in the URL is ignored. */
const CODE_RE = /^[a-z][a-z0-9_]{1,63}$/;

export function savePendingPlan(code: string): void {
  if (!CODE_RE.test(code)) return;
  try {
    localStorage.setItem(KEY, code);
  } catch {
    // storage unavailable — the preselection is a nicety, not required
  }
}

export function getPendingPlan(): string | null {
  try {
    const code = localStorage.getItem(KEY);
    return code && CODE_RE.test(code) ? code : null;
  } catch {
    return null;
  }
}

export function clearPendingPlan(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}

/** Reads `?plan=` from a query string and parks it. Returns the saved code, if any. */
export function capturePlanFromSearch(search: string): string | null {
  const code = new URLSearchParams(search).get("plan");
  if (!code || !CODE_RE.test(code)) return null;
  savePendingPlan(code);
  return code;
}
