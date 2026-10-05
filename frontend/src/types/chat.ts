/** Types for the Chat Inbox + Payments verification APIs (mirror the backend schemas). */

export type Channel = "whatsapp" | "instagram" | "website" | string;
export type SenderType = "customer" | "bot" | "human" | "system";
export type ProofStatus = "pending" | "approved" | "rejected";

export interface LastMessage {
  text: string;
  direction: "inbound" | "outbound";
  sender_type: SenderType;
  media_type: string | null;
  created_at: string | null;
}

export interface InboxItem {
  id: number;
  customer_name: string | null;
  phone_number: string;
  channel: Channel;
  lead_status: string;
  last_message: LastMessage | null;
  unread_count: number;
  payment_to_verify: boolean;
  bot_paused: boolean;
  ai_enabled: boolean;
  updated_at: string | null;
}

export type InboxFilter = "all" | "unread" | "payment_to_verify" | "bot_paused";

export interface ProofCard {
  proof_id: number;
  order_id: number;
  order_number: string;
  amount_expected: number;
  status: ProofStatus;
  order_status: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  reject_reason: string | null;
}

export interface ChatMessage {
  id: number;
  direction: "inbound" | "outbound";
  sender_type: SenderType;
  sender_name: string | null;
  text: string;
  media_url: string | null;
  media_type: string | null;
  created_at: string;
  payment_proof: ProofCard | null;
  /** client-side only: optimistic send state */
  localId?: string;
  pending?: boolean;
  failed?: boolean;
  failureCode?: string;
}

export interface OrderEvent {
  order_id: number;
  order_number: string;
  status: string;
  at: string;
}

export interface OrderBrief {
  id: number;
  order_number: string;
  status: string;
  total_amount: number;
  created_at: string | null;
  items: string[];
}

export interface WindowState {
  open: boolean;
  closes_at: string | null;
  seconds_left: number;
}

export interface ChatDetail {
  id: number;
  phone_number: string;
  channel: Channel;
  customer_name: string | null;
  ai_enabled: boolean;
  bot_paused: boolean;
  bot_pause_source: string | null;
  auto_resume_at: string | null;
  taken_over_at: string | null;
  taken_over_note: string | null;
  lead_status: string;
  message_count: number;
  created_at: string;
  updated_at: string | null;
  window: WindowState;
  opted_out: boolean;
  messages: ChatMessage[];
  has_more: boolean;
  events: OrderEvent[];
  current_order: OrderBrief | null;
  orders: OrderBrief[];
}

export interface ApprovedTemplate {
  id: number;
  name: string;
  language: string;
  category: string;
  body: string;
}

export interface SendResult {
  message: ChatMessage;
  bot_paused: boolean;
  auto_resume_at: string | null;
}

/** Structured error body the backend returns as `detail`. */
export interface ApiErrorDetail {
  code: string;
  message?: string;
  templates?: ApprovedTemplate[];
}

export interface PaymentProofRow {
  id: number;
  message_id: number | null;
  media_url: string;
  status: ProofStatus;
  created_at: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  reject_reason: string | null;
}

export interface PaymentRow {
  order_id: number;
  order_number: string;
  order_status: string;
  conversation_id: number | null;
  channel: Channel | null;
  customer_name: string;
  customer_phone: string;
  amount_expected: number;
  currency: string;
  items: { name: string; variant: string | null; quantity: number; subtotal: number }[];
  proofs: PaymentProofRow[];
  waiting_since: string | null;
  waiting_seconds: number | null;
  overdue: boolean;
  seller_upi_id: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  reject_reason: string | null;
}

export interface DecisionResult {
  order_id: number;
  order_number: string;
  status: string;
  changed: boolean;
  customer_notified: boolean | null;
  notify_failure: string | null;
}

export interface PaymentSettings {
  upi_id: string | null;
  upi_payee_name: string | null;
  upi_qr_url: string | null;
  upi_configured: boolean;
  setup_alert_active: boolean;
  bot_auto_resume_minutes: number;
  payment_expiry_hours: number;
  preview: { en: string; hi: string; gu: string };
}

/** Realtime events pushed over SSE (or reconstructed by /events/poll). */
export type RealtimeEvent =
  | { type: "new_message"; data: { conversation_id: number; message: ChatMessage } }
  | { type: "payment_submitted"; data: { order_id: number; conversation_id: number; proof_id: number; additional?: boolean } }
  | { type: "payment_reviewed"; data: { order_id: number; conversation_id: number | null; decision: string; reviewed_by?: string | null; reason?: string | null } }
  | { type: "conversation_updated"; data: { conversation_id: number; bot_paused: boolean } }
  | { type: "payment_setup_required"; data: { conversation_id: number | null } };
