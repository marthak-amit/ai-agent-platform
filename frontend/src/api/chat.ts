import { api } from "./client";
import type {
  ApiErrorDetail,
  ApprovedTemplate,
  ChatDetail,
  DecisionResult,
  InboxFilter,
  InboxItem,
  PaymentRow,
  PaymentSettings,
  ProofStatus,
  SendResult,
} from "../types/chat";

/** Extract the backend's structured `{code, message, ...}` error from an axios error. */
export function apiError(err: unknown): ApiErrorDetail {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (detail && typeof detail === "object" && "code" in (detail as object)) return detail as ApiErrorDetail;
  if (typeof detail === "string") return { code: "ERROR", message: detail };
  return { code: "NETWORK", message: (err as Error)?.message ?? "Network error" };
}

// ── Inbox ────────────────────────────────────────────────────────────────────

export async function fetchInbox(params: { filter: InboxFilter; search?: string; limit?: number; offset?: number }) {
  const { data } = await api.get<InboxItem[]>("/conversations", { params });
  return data;
}

export async function fetchChat(id: number, beforeId?: number) {
  const { data } = await api.get<ChatDetail>(`/conversations/${id}`, { params: beforeId ? { before_id: beforeId } : {} });
  return data;
}

export async function markChatRead(id: number) {
  await api.post(`/conversations/${id}/read`);
}

export async function uploadChatMedia(id: number, file: File) {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post<{ media_url: string; media_type: string }>(`/conversations/${id}/media`, form);
  return data;
}

export interface SendPayload {
  text?: string;
  media_url?: string;
  caption?: string;
  template?: { name: string; language: string; variables: string[] };
  client_msg_id: string;
}

export async function sendChatMessage(id: number, payload: SendPayload) {
  const { data } = await api.post<SendResult>(`/conversations/${id}/messages`, payload);
  return data;
}

export async function pauseBot(id: number, note?: string) {
  const { data } = await api.patch<ChatDetail>(`/conversations/${id}/takeover`, { note });
  return data;
}

export async function resumeBot(id: number) {
  const { data } = await api.patch<ChatDetail>(`/conversations/${id}/resume`);
  return data;
}

export async function fetchApprovedTemplates() {
  const { data } = await api.get<ApprovedTemplate[]>("/conversations/templates/approved");
  return data;
}

// ── Payments ─────────────────────────────────────────────────────────────────

export async function fetchPayments(params: {
  status: ProofStatus;
  from?: string;
  to?: string;
  search?: string;
  limit?: number;
  offset?: number;
}) {
  const { data } = await api.get<PaymentRow[]>("/payments/pending", { params });
  return data;
}

export async function fetchPendingCount() {
  const { data } = await api.get<{ count: number }>("/payments/pending/count");
  return data.count;
}

export async function approvePayment(orderId: number) {
  const { data } = await api.post<DecisionResult>(`/orders/${orderId}/payment/approve`);
  return data;
}

export async function rejectPayment(orderId: number, reason?: string) {
  const { data } = await api.post<DecisionResult>(`/orders/${orderId}/payment/reject`, { reason: reason || undefined });
  return data;
}

// ── Payment settings ─────────────────────────────────────────────────────────

export async function fetchPaymentSettings() {
  const { data } = await api.get<PaymentSettings>("/settings/payment");
  return data;
}

export async function savePaymentSettings(body: {
  upi_id?: string;
  upi_payee_name?: string;
  bot_auto_resume_minutes?: number;
  payment_expiry_hours?: number;
}) {
  const { data } = await api.put<PaymentSettings>("/settings/payment", body);
  return data;
}

export async function uploadPaymentQr(file: File) {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post<PaymentSettings>("/settings/payment/qr", form);
  return data;
}

export async function deletePaymentQr() {
  const { data } = await api.delete<PaymentSettings>("/settings/payment/qr");
  return data;
}

/** Full URL for an image the backend returned (local /uploads paths need the API origin in prod). */
export function mediaSrc(url: string | null | undefined): string {
  if (!url) return "";
  if (/^https?:\/\//.test(url)) return url;
  const base = (import.meta.env.VITE_API_URL as string | undefined) ?? "";
  if (base.startsWith("http")) return `${base.replace(/\/$/, "")}${url}`;
  return `/api${url}`;
}
