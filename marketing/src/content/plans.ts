// Single source of truth for plan pricing on the marketing site (cards, meta tags, JSON-LD, FAQ).
// `code` must match billing_plans.code in the backend (backend/alembic/versions/0062_*), because the
// dashboard preselects the plan from ?plan=<code> after signup.

/**
 * Whether the listed prices already include GST. MUST mirror the backend's PRICES_INCLUDE_GST
 * (backend/app/config.py, default false = GST is added on top at checkout). If you flip it there,
 * flip it here so the page never advertises a different total than Razorpay charges.
 */
export const PRICES_INCLUDE_GST = false;
export const GST_RATE_PERCENT = 18;

export const GST_LINE = PRICES_INCLUDE_GST
  ? `Prices inclusive of ${GST_RATE_PERCENT}% GST`
  : `Prices exclusive of ${GST_RATE_PERCENT}% GST — GST is added at checkout`;

export interface PlanSpec {
  code: string;
  name: string;
  conversationsPerMonth: number;
  priceInr: number;
  instagram: boolean;
  popular?: boolean;
}

export const PLAN_SPECS: PlanSpec[] = [
  { code: "starter_1500", name: "Starter", conversationsPerMonth: 1500, priceInr: 4599, instagram: false },
  { code: "growth_5000", name: "Growth", conversationsPerMonth: 5000, priceInr: 11999, instagram: true, popular: true },
  { code: "pro_9000", name: "Pro", conversationsPerMonth: 9000, priceInr: 17999, instagram: true },
];

const inr = new Intl.NumberFormat("en-IN");

/** 4599 → "₹4,599" (Indian digit grouping). */
export function formatInr(amount: number): string {
  return `₹${inr.format(amount)}`;
}

/** Price per conversation, e.g. 4599 / 1500 → "₹3.07". */
export function perConversation(spec: PlanSpec): string {
  return `₹${(spec.priceInr / spec.conversationsPerMonth).toFixed(2)}`;
}

/** "Starter ₹4,599, Growth ₹11,999, Pro ₹17,999" — for meta descriptions. */
export const PRICE_SUMMARY = PLAN_SPECS.map((p) => `${p.name} ${formatInr(p.priceInr)}`).join(", ");

/** Short form shown under each price on a card. */
export const GST_LINE_SHORT = PRICES_INCLUDE_GST ? `incl. ${GST_RATE_PERCENT}% GST` : `+ ${GST_RATE_PERCENT}% GST`;
