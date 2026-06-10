export interface ClientProfile {
  id: number;
  email: string;
  business_name: string;
  phone: string | null;
  gemini_system_prompt: string;
  gst_number: string | null;
  business_address: string | null;
  hsn_code: string | null;
  briefing_enabled: boolean;
  briefing_time: string;
  dashboard_language: string;
  plan_slug: string;
  catalogue_slug: string | null;
  catalogue_tagline: string | null;
  catalogue_theme_color: string;
  logo_url: string | null;
  banner_url: string | null;
  onboarding_step: number;
  onboarding_completed: boolean;
  business_type: string | null;
  business_description: string | null;
  whatsapp_number: string | null;
  whatsapp_phone_number_id: string | null;
  whatsapp_access_token: string | null;
}

export interface ProductVariant {
  id: number;
  color: string | null;
  size: string | null;
  material: string | null;
  sku: string | null;
  price: number | null;
  stock: number;
  is_active: boolean;
  image_url: string | null;
}

export interface Product {
  id: number;
  client_id: number;
  name: string;
  price: number;
  stock: number | null;
  description: string | null;
  image_url: string | null;
  sku: string | null;
  category: string | null;
  is_active: boolean;
  low_stock_alert: number;
  has_variants: boolean;
  variants: ProductVariant[];
  available_colors: string[];
  available_sizes: string[];
}

export interface StockLog {
  id: number;
  product_id: number;
  adjustment: number;
  reason: string;
  stock_before: number;
  stock_after: number;
  created_at: string;
}

export interface UsageStat {
  today_count: number;
  monthly_count: number;
  limit: number;
  percentage_used: number;
}

export interface Plan {
  slug: string;
  name: string;
  price_inr: number;
  daily_msg_limit: number;
  channels: string[];
  description: string;
}

export interface OnboardingStatus {
  completion_percentage: number;
  steps_done: string[];
  steps_pending: string[];
}

export interface ConversationSummary {
  id: number;
  phone_number: string;
  channel: "whatsapp" | "instagram" | string;
  lead_status: "hot" | "warm" | "cold";
  last_message: string;
  message_count: number;
  ai_enabled: boolean;
  updated_at: string | null;
  current_stage?: string;
}

export interface Message {
  id: number;
  role: "user" | "model" | "human";
  content: string;
  created_at: string;
  original_type?: string;
}

export interface ConversationDetail {
  id: number;
  phone_number: string;
  channel: string;
  ai_enabled: boolean;
  taken_over_at: string | null;
  taken_over_note: string | null;
  lead_status: string;
  message_count: number;
  created_at: string;
  updated_at: string | null;
  messages: Message[];
  current_stage?: string;
}

export interface Lead {
  id: number;
  phone_number: string;
  status: "hot" | "warm" | "cold";
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface Order {
  id: number;
  order_number: string;
  client_id: number;
  conversation_id: number | null;
  customer_name: string;
  customer_phone: string;
  delivery_address: string;
  product_id: number | null;
  product_name: string;
  product_sku: string | null;
  variant_color: string | null;
  variant_size: string | null;
  quantity: number;
  unit_price: number;
  total_amount: number;
  payment_method: string;
  payment_status: string;
  razorpay_payment_id: string | null;
  status: string;
  tracking_number: string | null;
  courier_name: string | null;
  created_at: string | null;
  confirmed_at: string | null;
  paid_at: string | null;
  dispatched_at: string | null;
  delivered_at: string | null;
  notes: string | null;
  invoice_url: string | null;
  invoice_number: string | null;
}

export interface OrderStats {
  today_orders: number;
  today_revenue: number;
  pending_dispatch: number;
  cod_pending: number;
  total_orders: number;
  total_revenue: number;
}
