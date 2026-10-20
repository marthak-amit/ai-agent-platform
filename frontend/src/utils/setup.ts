import type { ChecklistKey } from "../types/setup";

/**
 * Where each checklist item sends the owner. The first four reuse the pages that used to be wizard steps
 * (/setup/*); "Test your agent" is the chat simulator; "Go live" is an action on the dashboard card.
 */
export const CHECKLIST_ROUTES: Record<ChecklistKey, string> = {
  add_products: "/setup/products",
  setup_agent: "/setup/agent",
  connect_whatsapp: "/setup/whatsapp",
  connect_instagram: "/setup/instagram",
  test_agent: "/sandbox",
  go_live: "/dashboard?golive=1",
};

/** Days left of a running trial (rounded up, never negative); null when there is no end date. */
export function trialDaysLeft(endsAt: string | null | undefined, now: number = Date.now()): number | null {
  if (!endsAt) return null;
  const ms = new Date(endsAt).getTime() - now;
  if (Number.isNaN(ms)) return null;
  return Math.max(0, Math.ceil(ms / 86_400_000));
}
