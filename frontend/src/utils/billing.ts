import type {
  Plan,
  PaymentOrderStatus,
  SubscriptionState,
  UpgradeOption,
} from "../types/billing";

const RUPEE = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
});

/** Paise → "₹4,599" (Indian digit grouping); decimals only when the amount has paise ("₹5,426.82"). */
export function formatPaise(paise: number): string {
  const rupees = paise / 100;
  if (Number.isInteger(rupees)) return RUPEE.format(rupees);
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(rupees);
}

/** Per-conversation price in paise, rounded to the nearest paisa; 0 when the limit is not positive. */
export function perConversationPaise(basePaise: number, conversationLimit: number): number {
  if (conversationLimit <= 0) return 0;
  return Math.round(basePaise / conversationLimit);
}

const DATE_LOCALES: Record<string, string> = { en: "en-IN", hi: "hi-IN", gu: "gu-IN" };

/** "12 Nov 2026" in the dashboard language (falls back to en-IN). */
export function formatDate(iso: string | null | undefined, lang: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(DATE_LOCALES[lang] ?? "en-IN", { day: "numeric", month: "short", year: "numeric" });
}

export type UsageTone = "ok" | "warn" | "over";

/** Usage bar colour: green below 80 %, amber 80–100 %, coral above 100 %. */
export function usageTone(percentUsed: number): UsageTone {
  if (percentUsed > 100) return "over";
  if (percentUsed >= 80) return "warn";
  return "ok";
}

/** Plan card button state. */
export type PlanAction =
  | { kind: "subscribe" }
  | { kind: "renew" }
  | { kind: "upgrade"; option: UpgradeOption | null }
  | { kind: "downgrade_blocked" };

/**
 * Decides which button a plan card shows. Mirrors the backend rule in `build_quote`: no active plan → new
 * purchase; same plan → renewal (queued after the running period); higher `sort_order` → upgrade with credit;
 * lower `sort_order` → blocked until the period ends.
 */
export function planAction(plan: Plan, plans: Plan[], state: SubscriptionState | null): PlanAction {
  const activeCode = state?.subscription?.plan.code;
  if (!state || state.status !== "active" || !activeCode) return { kind: "subscribe" };
  if (plan.code === activeCode) return { kind: "renew" };
  const active = plans.find((p) => p.code === activeCode);
  if (active && plan.sort_order < active.sort_order) return { kind: "downgrade_blocked" };
  return { kind: "upgrade", option: state.upgrade_options.find((o) => o.plan_code === plan.code) ?? null };
}

/** Identity of what a tenant currently owns; it changes when a payment activates (new/upgrade/renewal). */
export function subscriptionFingerprint(state: SubscriptionState | null): string {
  if (!state || !state.subscription) return "none";
  const s = state.subscription;
  return [s.id, s.plan.code, s.current_period_end, ...state.queued.map((q) => q.id)].join("|");
}

export type BannerKind = "usage_high" | "over_limit" | "expiring" | "grace" | "expired";

export interface Banner {
  kind: BannerKind;
  /** Red/coral banners are not dismissible; amber ones can be hidden for the session. */
  severity: "warning" | "critical";
  /** Stable per state, so a dismissed banner comes back when the underlying situation changes. */
  key: string;
  params: Record<string, string | number>;
}

/** Days-left threshold below which the "expiring" banner shows. */
export const EXPIRING_DAYS = 3;

/**
 * Banners for the app layout, most severe first. Billing-exempt accounts and accounts whose state is not loaded
 * get none. Expired/grace are mutually exclusive with the in-period banners (no running period then).
 */
export function deriveBanners(state: SubscriptionState | null): Banner[] {
  const ent = state?.entitlement;
  if (!state || !ent || ent.state === "exempt") return [];

  if (ent.state === "expired") {
    return [{ kind: "expired", severity: "critical", key: "expired", params: { restricted: ent.restricted ? 1 : 0 } }];
  }
  if (ent.state === "grace") {
    return [{ kind: "grace", severity: "critical", key: `grace:${ent.grace_ends_at ?? ""}`, params: { until: ent.grace_ends_at ?? "" } }];
  }

  const sub = state.subscription;
  if (!sub) return [];
  const out: Banner[] = [];
  const pct = sub.percent_used;
  const params = { plan: sub.plan.name, used: sub.conversations_used, limit: sub.conversation_limit, pct: Math.round(pct) };

  if (state.over_limit || ent.over_limit || pct > 100) {
    out.push({ kind: "over_limit", severity: "critical", key: `over:${sub.id}`, params });
  } else if (pct >= 80) {
    out.push({ kind: "usage_high", severity: "warning", key: `usage:${sub.id}`, params });
  }
  // Expiring only matters when nothing is queued to take over.
  if (sub.days_left <= EXPIRING_DAYS && state.queued.length === 0) {
    out.push({
      kind: "expiring",
      severity: "warning",
      key: `expiring:${sub.id}:${sub.days_left}`,
      params: { plan: sub.plan.name, days: sub.days_left, end: sub.current_period_end },
    });
  }
  return out;
}

export type StatusTone = "success" | "danger" | "pending" | "neutral";

/** Visual tone for a payment-order status. */
export function paymentStatusTone(status: PaymentOrderStatus): StatusTone {
  switch (status) {
    case "paid":
      return "success";
    case "failed":
      return "danger";
    case "created":
    case "attempted":
      return "pending";
    default:
      return "neutral";
  }
}
