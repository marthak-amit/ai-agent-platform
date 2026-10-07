// Types for the /admin panel API (see backend/app/schemas/admin_panel.py).

export type AdminPermission =
  | "overview.read"
  | "clients.read"
  | "clients.write"
  | "clients.impersonate"
  | "conversations.read"
  | "billing.read"
  | "billing.write"
  | "plans.write"
  | "usage.read"
  | "system.read"
  | "audit.read"
  | "admins.manage";

export type AdminRole = "superadmin" | "support" | "billing" | "viewer";

export interface AdminMe {
  id: number | null;
  email: string;
  name: string;
  role: AdminRole;
  permissions: AdminPermission[];
  totp_enabled: boolean;
  must_change_password: boolean;
  last_login_at: string | null;
  via: "jwt" | "key";
}

export interface AdminToken {
  access_token: string;
  token_type: string;
  expires_in: number;
  admin: AdminMe;
}

export interface Paged<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface Overview {
  clients: { total: number; active: number; suspended: number; new_7d: number; new_30d: number };
  revenue: { collected_30d_inr: number; paid_orders_30d: number; active_subscriptions: number; expiring_7d: number };
  messages: { today: number; this_month: number };
  llm_30d: { calls: number; cost_inr: number; tokens: number; failures: number };
  attention: {
    payments_awaiting_review: number;
    bots_paused: number;
    billing_webhook_errors: number;
    over_limit_clients: number;
  };
  signups_14d: { date: string; count: number }[];
  llm_daily_14d: { date: string; cost_inr: number; calls: number }[];
}

export interface ClientRow {
  id: number;
  email: string;
  business_name: string;
  phone: string | null;
  is_active: boolean;
  billing_exempt: boolean;
  plan_slug: string;
  sub_plan: string | null;
  sub_status: string | null;
  sub_period_end: string | null;
  conversations_used: number | null;
  conversation_limit: number | null;
  whatsapp_connected: boolean;
  instagram_connected: boolean;
  created_at: string | null;
}

export interface AuditEntry {
  id: number;
  actor: string;
  admin_user_id: number | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  client_id: number | null;
  reason: string;
  detail: Record<string, unknown>;
  ip: string | null;
  success: boolean;
  created_at: string;
}

export interface ClientDetail {
  profile: {
    id: number;
    email: string;
    business_name: string;
    phone: string | null;
    business_type: string | null;
    whatsapp_number: string | null;
    catalogue_slug: string | null;
    gst_number: string | null;
    is_active: boolean;
    onboarding_completed: boolean;
    created_at: string | null;
  };
  flags: {
    router_v2_enabled: boolean | null;
    billing_exempt: boolean;
    plan_grandfathered: boolean;
    daily_message_limit: number;
    bot_auto_resume_minutes: number;
    plan_slug: string;
  };
  channels: {
    whatsapp_phone_number_id: string | null;
    whatsapp_token_set: boolean;
    instagram_account_id: string | null;
    instagram_token_set: boolean;
    upi_configured: boolean;
    razorpay_configured: boolean;
  };
  billing: {
    active_subscription: {
      id: number;
      plan_code: string;
      plan_name: string;
      status: string;
      period_start: string;
      period_end: string;
      conversations_used: number;
      conversation_limit: number;
      over_limit: boolean;
    } | null;
    queued_periods: number;
  };
  users: { id: number; email: string; role: string; is_active: boolean; created_at: string; invited: boolean }[];
  counts: {
    conversations: number;
    paused_conversations: number;
    orders: number;
    orders_awaiting_payment_review: number;
    products: number;
  };
  usage: { messages_today: number; messages_this_month: number };
  llm_30d: { calls: number; cost_inr: number; failures: number };
  recent_audit: AuditEntry[];
}

export interface FlagsUpdate {
  router_v2_enabled?: boolean | null;
  daily_message_limit?: number;
  bot_auto_resume_minutes?: number;
  plan_grandfathered?: boolean;
  reason?: string;
}

export interface ConversationRow {
  id: number;
  phone_number: string;
  channel: string;
  current_stage: string;
  ai_enabled: boolean;
  bot_pause_source: string | null;
  customer_name: string | null;
  is_sandbox: boolean;
  message_count: number;
  last_message_at: string | null;
  created_at: string;
}

export interface MessageRow {
  id: number;
  role: string;
  direction: string | null;
  sender_type: string | null;
  content: string;
  media_type: string | null;
  created_at: string;
}

export interface OrderRow {
  id: number;
  order_number: string;
  customer_name: string;
  customer_phone: string;
  product_name: string;
  quantity: number;
  total_amount: number;
  payment_method: string;
  payment_status: string;
  status: string;
  created_at: string;
  paid_at: string | null;
}

export interface Impersonation {
  access_token: string;
  expires_in: number;
  client_id: number;
  email: string;
}

export interface BillingPlan {
  id: number;
  code: string;
  name: string;
  conversation_limit: number;
  price_paise: number;
  currency: string;
  billing_period_days: number;
  features: Record<string, unknown>;
  is_active: boolean;
  sort_order: number;
  active_subscribers: number;
}

export interface LegacyPlan {
  plan_id: string;
  name: string;
  price_inr: number;
  conv_limit: number;
  image_quota: number;
  image_overage_price: number;
  daily_msg_limit: number;
  channels: string[];
  campaign_allowed: boolean;
  campaign_max_recipients: number;
  campaign_monthly_limit: number;
  tier_order: number;
  description: string;
  is_active: boolean;
}

export interface Subscription {
  id: number;
  client_id: number;
  client_email: string;
  business_name: string;
  billing_exempt: boolean;
  plan_code: string;
  plan_name: string;
  status: string;
  current_period_start: string;
  current_period_end: string;
  conversations_used: number;
  conversation_limit: number;
  over_limit: boolean;
  source_payment_id: number | null;
  created_at: string;
}

export interface PaymentEvent {
  id: number;
  razorpay_event_id: string;
  event_type: string;
  processed: boolean;
  error: string | null;
  created_at: string;
  processed_at: string | null;
  payload?: unknown;
}

export interface Revenue {
  month: string;
  total_revenue_inr: number;
  breakdown: { plan: string; plan_name: string; client_count: number; revenue_inr: number }[];
}

export interface UsageMetrics {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_inr: number;
  failures: number;
  avg_latency_ms: number;
}

export interface LLMUsageReport {
  from_date: string;
  to_date: string;
  timezone: string;
  client_id: number | null;
  totals: UsageMetrics;
  per_day: (UsageMetrics & { day: string })[];
  per_purpose: (UsageMetrics & { purpose: string })[];
  per_model: (UsageMetrics & { model: string })[];
  per_conversation: (UsageMetrics & { conversation_id: number })[];
  per_order: (UsageMetrics & { order_id: number; order_number: string | null })[];
}

export interface SystemHealth {
  status: "healthy" | "degraded";
  checks: Record<string, string>;
  llm: {
    available: boolean;
    reason: string | null;
    failures_last_hour: number;
    vision_enabled: boolean;
    vision_disabled_reason: string | null;
    missing_models: Record<string, string>;
  };
  instagram: { disabled: boolean; client_verdicts: Record<string, number>; invalid_clients: number[] };
  scheduler: { id: string; trigger: string; next_run_time: string | null }[];
  config: {
    environment: string;
    router_v2_client_ids: string;
    llm_models: Record<string, string>;
    admin_api_key_enabled: boolean;
    secrets_configured: Record<string, boolean>;
  };
  billing: {
    last_webhook_at?: string | null;
    webhook_errors_24h?: number;
    jobs?: { name: string; last_finished_at: string | null; status: string }[];
    error?: string;
  };
  timestamp: string;
}

export interface Operator {
  id: number;
  email: string;
  name: string;
  role: AdminRole;
  is_active: boolean;
  totp_enabled: boolean;
  must_change_password: boolean;
  locked_until: string | null;
  last_login_at: string | null;
  last_login_ip: string | null;
  created_by: string | null;
  created_at: string;
}

export interface OperatorCreated {
  admin: Operator;
  temporary_password: string;
}
