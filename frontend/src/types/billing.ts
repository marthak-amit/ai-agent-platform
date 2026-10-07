/** Response/request types for the SellerTalk24 billing API (`/billing/*`). All money is integer paise. */

export interface Amounts {
  base_paise: number;
  credit_paise: number;
  taxable_paise: number;
  cgst_paise: number;
  sgst_paise: number;
  igst_paise: number;
  gst_paise: number;
  total_paise: number;
}

export interface PlanFeatures {
  whatsapp?: boolean;
  instagram?: boolean;
  [key: string]: unknown;
}

export interface Plan extends Amounts {
  code: string;
  name: string;
  conversation_limit: number;
  billing_period_days: number;
  features: PlanFeatures;
  sort_order: number;
  is_current: boolean;
  prices_include_gst: boolean;
  gst_rate_bps: number;
}

export interface PlanList {
  plans: Plan[];
}

export interface SubscriptionPlan {
  code: string;
  name: string;
  features: PlanFeatures;
}

export interface Subscription {
  id: number;
  status: string;
  plan: SubscriptionPlan;
  current_period_start: string;
  current_period_end: string;
  conversations_used: number;
  conversation_limit: number;
  percent_used: number;
  days_left: number;
}

export interface QueuedPeriod {
  id: number;
  plan_code: string;
  plan_name: string;
  current_period_start: string;
  current_period_end: string;
}

export interface UpgradeOption {
  plan_code: string;
  plan_name: string;
  conversation_limit: number;
  amounts: Amounts;
}

export type EntitlementKind = "active" | "exempt" | "grace" | "expired";

export interface Entitlement {
  state: EntitlementKind;
  grace_ends_at: string | null;
  enforced: boolean;
  restricted: boolean;
  over_limit: boolean;
}

export interface SubscriptionState {
  status: "active" | "none";
  entitlement: Entitlement | null;
  over_limit: boolean;
  /** GST state code of the seller ("24" = Gujarat): a buyer GSTIN in the same state is taxed CGST+SGST, otherwise IGST. */
  seller_state_code: string;
  subscription: Subscription | null;
  queued: QueuedPeriod[];
  upgrade_options: UpgradeOption[];
}

export type PaymentPurpose = "new" | "renewal" | "upgrade";

export interface CheckoutResponse {
  key_id: string;
  razorpay_order_id: string;
  /** Total to charge, in paise. */
  amount: number;
  currency: string;
  name: string;
  description: string;
  prefill: { name: string; email: string; contact: string };
  mock: boolean;
  payment_order_id: number;
  purpose: PaymentPurpose;
  plan_code: string;
  amounts: Amounts;
}

/** The three values Checkout.js hands to its success handler; also the body of POST /billing/verify. */
export interface VerifyPayload {
  razorpay_order_id: string;
  razorpay_payment_id: string;
  razorpay_signature: string;
}

export type PaymentOrderStatus = "created" | "attempted" | "paid" | "failed" | "refunded";

export interface PaymentRow {
  id: number;
  plan_code: string;
  plan_name: string;
  purpose: PaymentPurpose;
  status: PaymentOrderStatus;
  razorpay_order_id: string;
  razorpay_payment_id: string | null;
  currency: string;
  amount_paise: number;
  base_paise: number;
  credit_paise: number;
  taxable_paise: number;
  cgst_paise: number;
  sgst_paise: number;
  igst_paise: number;
  gst_paise: number;
  failure_reason: string | null;
  created_at: string;
  paid_at: string | null;
  invoice_id: number | null;
  invoice_number: string | null;
}

export interface PaymentList {
  items: PaymentRow[];
  total: number;
  page: number;
  page_size: number;
}

/** Stable `{code, message}` error body used by the billing endpoints (some 5xx paths return a bare string). */
export interface BillingErrorDetail {
  code: string;
  message: string;
}

/** Minimal slice of the Razorpay Checkout.js API we rely on. */
export interface RazorpayOptions {
  key: string;
  order_id: string;
  amount: number;
  currency: string;
  name: string;
  description: string;
  prefill: { name: string; email: string; contact: string };
  theme: { color: string };
  handler: (response: VerifyPayload) => void;
  modal: { ondismiss: () => void };
}

export interface RazorpayFailure {
  error: { code?: string; description?: string; reason?: string };
}

export interface RazorpayInstance {
  open: () => void;
  on: (event: "payment.failed", cb: (response: RazorpayFailure) => void) => void;
}

declare global {
  interface Window {
    Razorpay?: new (options: RazorpayOptions) => RazorpayInstance;
  }
}
