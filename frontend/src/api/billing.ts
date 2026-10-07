import axios from "axios";
import { api } from "./client";
import type {
  BillingErrorDetail,
  CheckoutResponse,
  PaymentList,
  PlanList,
  SubscriptionState,
  VerifyPayload,
} from "../types/billing";

export async function fetchPlans(): Promise<PlanList> {
  const { data } = await api.get<PlanList>("/billing/plans");
  return data;
}

export async function fetchSubscription(): Promise<SubscriptionState> {
  const { data } = await api.get<SubscriptionState>("/billing/subscription");
  return data;
}

export async function fetchPayments(page = 1, pageSize = 20): Promise<PaymentList> {
  const { data } = await api.get<PaymentList>("/billing/payments", {
    params: { page, page_size: pageSize },
  });
  return data;
}

export async function createCheckout(planCode: string): Promise<CheckoutResponse> {
  const { data } = await api.post<CheckoutResponse>("/billing/checkout", { plan_code: planCode });
  return data;
}

export async function verifyPayment(payload: VerifyPayload): Promise<SubscriptionState> {
  const { data } = await api.post<SubscriptionState>("/billing/verify", payload);
  return data;
}

/** Mock mode only: simulate a successful payment for an order (404 when the backend has real keys). */
export async function mockComplete(razorpayOrderId: string): Promise<SubscriptionState> {
  const { data } = await api.post<SubscriptionState>("/billing/mock/complete", {
    razorpay_order_id: razorpayOrderId,
  });
  return data;
}

/** Fetches the invoice PDF (auth header required, so not a plain link) and saves it. Resolves false on failure. */
export async function downloadInvoicePdf(invoiceId: number, invoiceNumber: string): Promise<boolean> {
  try {
    const { data } = await api.get<Blob>(`/billing/invoices/${invoiceId}/pdf`, { responseType: "blob" });
    const url = URL.createObjectURL(new Blob([data], { type: "application/pdf" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${invoiceNumber.replace(/\//g, "-")}.pdf`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
    return true;
  } catch {
    return false;
  }
}

export interface ParsedApiError {
  /** Backend machine code (`downgrade_not_allowed`, `rate_limited`, …) when the body carries one. */
  code: string | null;
  /** Backend message when it carries one (it is English and client-safe). */
  message: string | null;
  /** HTTP status, or null for a network failure / timeout (no response received). */
  status: number | null;
  /** True when no response arrived at all — the request may or may not have reached the server. */
  isNetwork: boolean;
}

/** Normalises an axios error from the billing endpoints (`detail` is `{code, message}`, a string, or a 422 list). */
export function parseApiError(err: unknown): ParsedApiError {
  if (!axios.isAxiosError(err)) return { code: null, message: null, status: null, isNetwork: false };
  if (!err.response) return { code: null, message: null, status: null, isNetwork: true };
  const detail: unknown = err.response.data?.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as Partial<BillingErrorDetail>;
    return {
      code: typeof d.code === "string" ? d.code : null,
      message: typeof d.message === "string" ? d.message : null,
      status: err.response.status,
      isNetwork: false,
    };
  }
  return {
    code: null,
    message: typeof detail === "string" ? detail : null,
    status: err.response.status,
    isNetwork: false,
  };
}
