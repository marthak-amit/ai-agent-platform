import axios from "axios";
import type { Order, OrderStats } from "../types";

const BASE_URL = import.meta.env.VITE_API_URL ?? "/api";

export const api = axios.create({ baseURL: BASE_URL });

api.interceptors.request.use((config) => {
  const token = localStorage.getItem("access_token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// --- Auth ---

export async function login(email: string, password: string): Promise<string> {
  const { data } = await api.post<{ access_token: string }>("/auth/login", { email, password });
  return data.access_token;
}

export async function register(
  email: string,
  password: string,
  business_name: string,
  phone?: string,
) {
  const { data } = await api.post("/auth/register", { email, password, business_name, phone });
  return data;
}

export async function logout(): Promise<void> {
  await api.post("/auth/logout");
}

export async function getProfile() {
  const { data } = await api.get("/auth/me");
  return data;
}

export async function updateProfile(patch: {
  business_name?: string;
  phone?: string;
  gemini_system_prompt?: string;
  gst_number?: string;
  business_address?: string;
  hsn_code?: string;
  briefing_enabled?: boolean;
  briefing_time?: string;
  dashboard_language?: string;
  catalogue_slug?: string;
  catalogue_tagline?: string;
  catalogue_theme_color?: string;
  logo_url?: string;
  banner_url?: string;
  accepts_cod?: boolean;
  upi_id?: string;
}) {
  const { data } = await api.patch("/auth/me", patch);
  return data;
}

// --- Onboarding ---

export interface SetupAgentPayload {
  business_name: string;
  business_type: string;
  business_description: string;
  products?: { name: string; price: number; stock?: number }[];
  whatsapp_number?: string;
}

export async function setupAgent(payload: SetupAgentPayload) {
  const { data } = await api.post("/onboarding/setup-agent", payload);
  return data as { client_id: number; api_key: string; setup_status: object };
}

export async function getOnboardingStatus() {
  const { data } = await api.get("/onboarding/status");
  return data as { completion_percentage: number; steps_done: string[]; steps_pending: string[] };
}

export async function updateOnboardingStep(step: number) {
  const { data } = await api.patch("/onboarding/progress", { step });
  return data as { onboarding_step: number; onboarding_completed: boolean };
}

// --- Products / Catalogue ---

export interface VariantPayload {
  id?: number;
  color?: string;
  size?: string;
  stock: number;
  price?: number;
}

export interface ProductPayload {
  name: string;
  price: number;
  stock?: number;
  description?: string;
  image_url?: string;
  sku?: string;
  category?: string;
  is_active?: boolean;
  low_stock_alert?: number;
  has_variants?: boolean;
  variants?: VariantPayload[];
}

export async function listProducts(filters?: {
  category?: string;
  low_stock?: boolean;
  is_active?: boolean;
  search?: string;
}) {
  const { data } = await api.get("/catalogue/products", { params: filters });
  return data;
}

export async function addProduct(payload: ProductPayload) {
  const { data } = await api.post("/catalogue/products", payload);
  return data;
}

export async function updateProduct(id: number, payload: Partial<ProductPayload>) {
  const { data } = await api.put(`/catalogue/products/${id}`, payload);
  return data;
}

export async function deleteProduct(id: number): Promise<void> {
  await api.delete(`/catalogue/products/${id}`);
}

export async function uploadProductImage(file: File): Promise<string> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post<{ url: string }>("/catalogue/products/upload-image", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data.url;
}

export async function adjustStock(
  id: number,
  adjustment: number,
  reason: string,
) {
  const { data } = await api.post(`/catalogue/products/${id}/adjust-stock`, {
    adjustment,
    reason,
  });
  return data;
}

export async function adjustVariantStocks(
  id: number,
  adjustments: { variant_id: number; new_stock: number }[],
  reason: string,
) {
  const { data } = await api.post(`/catalogue/products/${id}/adjust-stock`, {
    adjustments,
    reason,
  });
  return data;
}

export async function getStockHistory(id: number) {
  const { data } = await api.get(`/catalogue/products/${id}/stock-history`);
  return data;
}

export async function searchProductBySku(sku: string) {
  const { data } = await api.get("/catalogue/products/search", { params: { sku } });
  return data;
}

// --- Usage ---

export async function getUsageStats() {
  const { data } = await api.get("/usage/stats");
  return data;
}

// --- Plans ---

export async function listPlans() {
  const { data } = await api.get("/plans");
  return data;
}

export async function getCurrentPlan() {
  const { data } = await api.get("/plans/current");
  return data;
}

export async function upgradePlan(plan_slug: string) {
  const { data } = await api.post("/plans/upgrade", { plan_slug });
  return data;
}

// --- Conversations ---

export async function getConversations(limit = 50) {
  const { data } = await api.get("/conversations", { params: { limit } });
  return data;
}

export async function getConversation(id: number) {
  const { data } = await api.get(`/conversations/${id}`);
  return data;
}

export async function takeoverConversation(id: number, note?: string) {
  const { data } = await api.patch(`/conversations/${id}/takeover`, { note });
  return data;
}

export async function resumeConversation(id: number) {
  const { data } = await api.patch(`/conversations/${id}/resume`);
  return data;
}

export async function sendHumanMessage(id: number, message: string) {
  const { data } = await api.post(`/conversations/${id}/send-message`, { message });
  return data;
}

// --- Leads ---

export async function getLeads(status?: string) {
  const { data } = await api.get("/leads", { params: status ? { status } : {} });
  return data;
}

export async function updateLeadStatus(id: number, status: string) {
  const { data } = await api.patch(`/leads/${id}`, { status });
  return data;
}

// --- Analytics ---

export async function getAnalyticsOverview() {
  const { data } = await api.get("/analytics/overview");
  return data;
}

export async function getLeadsFunnel() {
  const { data } = await api.get("/analytics/leads-funnel");
  return data;
}

export async function getTopQuestions() {
  const { data } = await api.get("/analytics/top-questions");
  return data;
}

// --- Channels ---

export async function testWhatsApp(): Promise<{ success: boolean; message: string }> {
  const { data } = await api.post("/channels/test-whatsapp");
  return data;
}

export async function updateChannelCredentials(payload: {
  whatsapp_phone_number_id?: string;
  whatsapp_access_token?: string;
  instagram_access_token?: string;
  instagram_account_id?: string;
}) {
  const { data } = await api.patch("/auth/me", payload);
  return data;
}

// --- Campaigns ---

export interface CampaignSummary {
  id: number;
  name: string;
  status: string;
  message_template: string;
  scheduled_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  total_recipients: number;
  sent_count: number;
  failed_count: number;
  delivered_count: number;
}

export interface CampaignRecipient {
  id: number;
  phone_number: string;
  customer_name: string | null;
  status: string;
  sent_at: string | null;
  error_message: string | null;
}

export interface CampaignDetail extends CampaignSummary {
  recipients: CampaignRecipient[];
}

export interface CampaignStats {
  total_recipients: number;
  sent_count: number;
  failed_count: number;
  delivered_count: number;
  pending_count: number;
  delivery_rate: number;
}

export async function getCampaigns(): Promise<CampaignSummary[]> {
  const { data } = await api.get("/campaigns");
  return data;
}

export async function createCampaign(payload: {
  name: string;
  message_template: string;
}): Promise<CampaignSummary> {
  const { data } = await api.post("/campaigns", payload);
  return data;
}

export async function addRecipients(
  id: number,
  payload: {
    recipients?: { phone: string; name?: string }[];
    import_from_conversations?: boolean;
  }
): Promise<{ added: number; total: number }> {
  const { data } = await api.post(`/campaigns/${id}/add-recipients`, payload);
  return data;
}

export async function getCampaignDetail(id: number): Promise<CampaignDetail> {
  const { data } = await api.get(`/campaigns/${id}`);
  return data;
}

export async function sendCampaign(id: number): Promise<{ status: string }> {
  const { data } = await api.post(`/campaigns/${id}/send`);
  return data;
}

export async function scheduleCampaign(
  id: number,
  scheduledAt: string
): Promise<{ status: string; scheduled_at: string }> {
  const { data } = await api.post(`/campaigns/${id}/schedule`, {
    scheduled_at: scheduledAt,
  });
  return data;
}

export async function getCampaignStats(id: number): Promise<CampaignStats> {
  const { data } = await api.get(`/campaigns/${id}/stats`);
  return data;
}

// --- Orders ---

export async function getOrders(params?: {
  status?: string;
  search?: string;
  date_from?: string;
  date_to?: string;
  skip?: number;
  limit?: number;
}): Promise<Order[]> {
  const { data } = await api.get("/orders", { params });
  return data;
}

export async function getOrderStats(): Promise<OrderStats> {
  const { data } = await api.get("/orders/stats");
  return data;
}

export async function getOrder(id: number): Promise<Order> {
  const { data } = await api.get(`/orders/${id}`);
  return data;
}

export async function updateOrderStatus(
  id: number,
  status: string,
  tracking_number?: string,
  courier_name?: string,
  notes?: string,
): Promise<Order> {
  const { data } = await api.patch(`/orders/${id}/status`, {
    status,
    tracking_number,
    courier_name,
    notes,
  });
  return data;
}

export async function notifyCustomer(id: number, message: string): Promise<{ status: string }> {
  const { data } = await api.post(`/orders/${id}/notify-customer`, { message });
  return data;
}

export async function exportOrdersCSV(): Promise<void> {
  const response = await api.get("/orders/export/csv", { responseType: "blob" });
  const url = window.URL.createObjectURL(new Blob([response.data]));
  const link = document.createElement("a");
  link.href = url;
  link.setAttribute("download", "orders.csv");
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}

// --- Customers ---

export interface CustomerRecord {
  id: number;
  client_id: number;
  phone: string;
  name: string | null;
  email: string | null;
  address: string | null;
  total_orders: number;
  total_spent: number;
  last_order_at: string | null;
  first_message_at: string | null;
  last_message_at: string | null;
  preferred_language: string;
  preferred_payment: string | null;
  is_vip: boolean;
  is_blocked: boolean;
  tags: string | null;
  notes: string | null;
  created_at: string | null;
}

export interface CustomerStats {
  total_customers: number;
  active_this_month: number;
  vip_customers: number;
  avg_order_value: number;
}

export async function getCustomers(params?: {
  search?: string;
  filter?: string;
  sort?: string;
  skip?: number;
  limit?: number;
}): Promise<CustomerRecord[]> {
  const { data } = await api.get("/customers", { params });
  return data;
}

export async function getCustomerStats(): Promise<CustomerStats> {
  const { data } = await api.get("/customers/stats");
  return data;
}

export async function getCustomerDetail(id: number): Promise<CustomerRecord> {
  const { data } = await api.get(`/customers/${id}`);
  return data;
}

export async function updateCustomer(
  id: number,
  patch: {
    name?: string;
    email?: string;
    address?: string;
    notes?: string;
    tags?: string;
    preferred_language?: string;
    preferred_payment?: string;
  }
): Promise<CustomerRecord> {
  const { data } = await api.patch(`/customers/${id}`, patch);
  return data;
}

export async function toggleCustomerVip(id: number): Promise<CustomerRecord> {
  const { data } = await api.post(`/customers/${id}/vip`);
  return data;
}

export async function toggleCustomerBlock(id: number): Promise<CustomerRecord> {
  const { data } = await api.post(`/customers/${id}/block`);
  return data;
}

export async function sendCustomerMessage(
  id: number,
  message: string
): Promise<{ status: string; phone: string }> {
  const { data } = await api.post(`/customers/${id}/message`, { message });
  return data;
}

export interface CustomerOrderRecord {
  id: number;
  order_number: string;
  product_name: string;
  quantity: number;
  total_amount: number;
  payment_method: string;
  status: string;
  created_at: string | null;
}

export interface CustomerConversationRecord {
  id: number;
  channel: string;
  current_stage: string;
  created_at: string | null;
  updated_at: string | null;
}

export async function getCustomerOrders(id: number): Promise<CustomerOrderRecord[]> {
  const { data } = await api.get(`/customers/${id}/orders`);
  return data;
}

export async function getCustomerConversations(id: number): Promise<CustomerConversationRecord[]> {
  const { data } = await api.get(`/customers/${id}/conversations`);
  return data;
}

export interface RevenueChartPoint {
  date: string;
  revenue: number;
}

export async function getRevenueChart(): Promise<RevenueChartPoint[]> {
  const { data } = await api.get("/analytics/revenue-chart");
  return data;
}

// --- Knowledge Base ---

export interface KBEntry {
  id: number;
  client_id: number;
  question: string;
  answer: string;
  source: string;
  category: string | null;
  language: string;
  usage_count: number;
  helpful_count: number;
  is_active: boolean;
  is_approved: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface KBStats {
  total: number;
  manual_count: number;
  auto_learned_count: number;
  active_count: number;
  most_used_question: string | null;
  most_used_count: number;
}

export async function getKnowledgeBase(params?: {
  source?: string;
  category?: string;
  active_only?: boolean;
}): Promise<KBEntry[]> {
  const { data } = await api.get("/knowledge", { params });
  return data;
}

export async function getKBStats(): Promise<KBStats> {
  const { data } = await api.get("/knowledge/stats");
  return data;
}

export async function addKnowledgeEntry(payload: {
  question: string;
  answer: string;
  category?: string;
  language?: string;
}): Promise<KBEntry> {
  const { data } = await api.post("/knowledge", payload);
  return data;
}

export async function updateKnowledgeEntry(
  id: number,
  payload: { question?: string; answer?: string; category?: string; language?: string }
): Promise<KBEntry> {
  const { data } = await api.put(`/knowledge/${id}`, payload);
  return data;
}

export async function deleteKnowledgeEntry(id: number): Promise<void> {
  await api.delete(`/knowledge/${id}`);
}

export async function toggleKnowledgeEntry(id: number): Promise<KBEntry> {
  const { data } = await api.patch(`/knowledge/${id}/toggle`);
  return data;
}

export async function uploadKnowledgeFile(file: File): Promise<{ entries_added: number; filename: string }> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post("/knowledge/upload", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}
