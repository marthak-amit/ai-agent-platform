import axios, { AxiosError } from "axios";
import type {
  AdminMe,
  AdminToken,
  AuditEntry,
  BillingPlan,
  ClientDetail,
  ClientRow,
  ConversationRow,
  FlagsUpdate,
  Impersonation,
  LegacyPlan,
  LLMUsageReport,
  MessageRow,
  Operator,
  OperatorCreated,
  OrderRow,
  Overview,
  Paged,
  PaymentEvent,
  Revenue,
  Subscription,
  SystemHealth,
} from "../types/admin";

const BASE_URL = import.meta.env.VITE_API_URL ?? "/api";

// Operator tokens live in sessionStorage (cleared when the browser closes) under their own key, and use their
// own axios instance — they never touch the tenant `api` client or its `access_token`.
const TOKEN_KEY = "admin_token";

export function getAdminToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setAdminToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: the session just won't survive a reload */
  }
}

export const adminApi = axios.create({ baseURL: BASE_URL });

adminApi.interceptors.request.use((config) => {
  const token = getAdminToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// A 401 anywhere (except the login call itself) means the session is gone: drop the token and tell the app.
adminApi.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    const url = error.config?.url ?? "";
    if (error.response?.status === 401 && !url.endsWith("/admin/auth/login")) {
      setAdminToken(null);
      window.dispatchEvent(new Event("admin-unauthorized"));
    }
    return Promise.reject(error);
  },
);

/** Error body the backend sends: `{detail: {code, message}}`, or FastAPI's `{detail: string | [...]}`. */
export interface ApiProblem {
  code: string;
  message: string;
  status: number;
}

export function problemFrom(err: unknown): ApiProblem {
  const e = err as AxiosError<{ detail?: unknown }>;
  const status = e.response?.status ?? 0;
  const detail = e.response?.data?.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail) && "code" in detail) {
    const d = detail as { code: string; message?: string };
    return { code: d.code, message: d.message ?? d.code, status };
  }
  if (typeof detail === "string") return { code: "error", message: detail, status };
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: string; loc?: unknown[] };
    return { code: "validation", message: `${first.loc?.slice(-1)[0] ?? "field"}: ${first.msg ?? "invalid"}`, status };
  }
  if (!e.response) return { code: "network", message: "Can't reach the server.", status: 0 };
  if (status >= 500) return { code: "server", message: `The server hit an error (${status}). Check the backend logs.`, status };
  return { code: "error", message: `Request failed (${status}).`, status };
}

// ── auth ─────────────────────────────────────────────────────────────────────

export async function adminLogin(email: string, password: string, otp?: string): Promise<AdminToken> {
  const { data } = await adminApi.post<AdminToken>("/admin/auth/login", { email, password, otp: otp || null });
  return data;
}
export const adminMe = async () => (await adminApi.get<AdminMe>("/admin/auth/me")).data;
export const adminLogout = async () => adminApi.post("/admin/auth/logout");
export const changeAdminPassword = async (current_password: string, new_password: string) =>
  (await adminApi.post<AdminToken>("/admin/auth/change-password", { current_password, new_password })).data;
export const totpSetup = async () =>
  (await adminApi.post<{ secret: string; otpauth_uri: string }>("/admin/auth/totp/setup")).data;
export const totpEnable = async (code: string) => adminApi.post("/admin/auth/totp/enable", { code });
export const totpDisable = async (password: string, code: string) =>
  adminApi.post("/admin/auth/totp/disable", { password, code });

// ── overview / clients ───────────────────────────────────────────────────────

export const getOverview = async () => (await adminApi.get<Overview>("/admin/panel/overview")).data;

export const searchClients = async (params: { q?: string; status?: string; page?: number; page_size?: number }) =>
  (await adminApi.get<Paged<ClientRow>>("/admin/panel/clients", { params })).data;

export const getClientDetail = async (id: number) =>
  (await adminApi.get<ClientDetail>(`/admin/panel/clients/${id}`)).data;

export const patchClientFlags = async (id: number, body: FlagsUpdate) =>
  (await adminApi.patch<{ changed: Record<string, unknown> }>(`/admin/panel/clients/${id}/flags`, body)).data;

export const suspendClient = async (id: number, reason: string) =>
  adminApi.put(`/admin/clients/${id}/suspend`, { reason });
export const activateClient = async (id: number, reason: string) =>
  adminApi.put(`/admin/clients/${id}/activate`, { reason });

export const getClientConversations = async (id: number, page = 1) =>
  (await adminApi.get<Paged<ConversationRow>>(`/admin/panel/clients/${id}/conversations`, { params: { page } })).data;
export const getConversationMessages = async (clientId: number, conversationId: number) =>
  (await adminApi.get<MessageRow[]>(`/admin/panel/clients/${clientId}/conversations/${conversationId}/messages`)).data;
export const getClientOrders = async (id: number, status = "", page = 1) =>
  (await adminApi.get<Paged<OrderRow>>(`/admin/panel/clients/${id}/orders`, { params: { status, page } })).data;
export const impersonateClient = async (id: number, reason: string) =>
  (await adminApi.post<Impersonation>(`/admin/panel/clients/${id}/impersonate`, { reason })).data;

// ── billing ──────────────────────────────────────────────────────────────────

export const getRevenue = async () => (await adminApi.get<Revenue>("/admin/revenue")).data;

export const getSubscriptions = async (params: { client_id?: number; status?: string; page?: number }) =>
  (await adminApi.get<Paged<Subscription>>("/admin/billing/subscriptions", { params })).data;
export const grantSubscription = async (
  clientId: number,
  body: { plan_code: string; days?: number; amount_paise?: number; reason: string },
) => (await adminApi.post<Subscription>(`/admin/billing/clients/${clientId}/grant`, body)).data;
export const extendSubscription = async (id: number, days: number, reason: string) =>
  (await adminApi.post<Subscription>(`/admin/billing/subscriptions/${id}/extend`, { days, reason })).data;
export const revokeSubscription = async (id: number, reason: string) =>
  (await adminApi.post<Subscription>(`/admin/billing/subscriptions/${id}/revoke`, { reason })).data;
export const setBillingExempt = async (clientId: number, exempt: boolean, reason: string) =>
  (await adminApi.put(`/admin/billing/clients/${clientId}/billing-exempt`, { exempt, reason })).data;

export const getPaymentEvents = async (params: { has_error?: boolean; processed?: boolean; page?: number }) =>
  (await adminApi.get<Paged<PaymentEvent>>("/admin/billing/payment-events", { params })).data;
export const getPaymentEvent = async (id: number) =>
  (await adminApi.get<PaymentEvent>(`/admin/billing/payment-events/${id}`)).data;
export const reprocessPaymentEvent = async (id: number) =>
  (await adminApi.post<{ outcome: string; processed: boolean; error: string | null }>(
    `/admin/billing/payment-events/${id}/reprocess`,
  )).data;
export const backfillInvoices = async () =>
  (await adminApi.post<{ created: number; invoice_numbers: string[] }>("/admin/billing/invoices/backfill")).data;

export const getBillingPlans = async () => (await adminApi.get<BillingPlan[]>("/admin/panel/billing-plans")).data;
export const updateBillingPlan = async (
  id: number,
  body: Partial<Pick<BillingPlan, "name" | "conversation_limit" | "price_paise" | "is_active" | "sort_order" | "features">> & {
    reason: string;
  },
) => (await adminApi.put<BillingPlan>(`/admin/panel/billing-plans/${id}`, body)).data;

export const getLegacyPlans = async () => (await adminApi.get<LegacyPlan[]>("/admin/plans")).data;
export const updateLegacyPlan = async (planId: string, body: Partial<LegacyPlan>) =>
  (await adminApi.put<LegacyPlan>(`/admin/plans/${planId}`, body)).data;

// ── usage / system ───────────────────────────────────────────────────────────

export const getLLMUsage = async (params: { client_id?: number; from?: string; to?: string }) =>
  (await adminApi.get<LLMUsageReport>("/admin/usage/llm", { params })).data;
export const getSystemHealth = async () => (await adminApi.get<SystemHealth>("/admin/panel/system")).data;

// ── operators / audit ────────────────────────────────────────────────────────

export const listOperators = async () => (await adminApi.get<Operator[]>("/admin/users")).data;
export const createOperator = async (body: { email: string; name: string; role: string }) =>
  (await adminApi.post<OperatorCreated>("/admin/users", body)).data;
export const updateOperator = async (id: number, body: { name?: string; role?: string; is_active?: boolean }) =>
  (await adminApi.patch<Operator>(`/admin/users/${id}`, body)).data;
export const resetOperatorPassword = async (id: number) =>
  (await adminApi.post<OperatorCreated>(`/admin/users/${id}/reset-password`)).data;
export const resetOperator2fa = async (id: number) => adminApi.post(`/admin/users/${id}/reset-2fa`);
export const unlockOperator = async (id: number) => adminApi.post(`/admin/users/${id}/unlock`);

export const getAuditLog = async (params: {
  actor?: string;
  action?: string;
  client_id?: number;
  success?: boolean;
  page?: number;
  page_size?: number;
}) => (await adminApi.get<Paged<AuditEntry>>("/admin/audit-log", { params })).data;
