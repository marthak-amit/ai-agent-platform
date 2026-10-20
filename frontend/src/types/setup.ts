/** Types for the dashboard-first onboarding: setup checklist, go-live and the in-dashboard chat simulator. */

export type ChecklistKey =
  | "add_products"
  | "setup_agent"
  | "connect_whatsapp"
  | "connect_instagram"
  | "test_agent"
  | "go_live";

export interface ChecklistItem {
  key: ChecklistKey;
  required: boolean;
  /** Derived from real state on the server (never ticked by hand). */
  done: boolean;
}

export interface TrialInfo {
  active: boolean;
  started_at: string | null;
  ends_at: string | null;
  conversations_used: number;
  conversation_limit: number;
}

/** What pressing "Go live" will do. */
export type GoLiveMode = "trial" | "plan" | "trial_used" | "live";

export interface Checklist {
  items: ChecklistItem[];
  done_count: number;
  total: number;
  all_required_done: boolean;
  hidden: boolean;
  next_key: ChecklistKey | null;
  account_status: "setup" | "trial" | "active";
  bot_active: boolean;
  quick_start_completed: boolean;
  demo_products: number;
  can_go_live: boolean;
  go_live_mode: GoLiveMode;
  trial: TrialInfo | null;
  /** Length / size of the free trial that "Go live" will start. */
  trial_days: number;
  trial_conversation_limit: number;
}

export interface GoLiveResult {
  account_status: string;
  bot_active: boolean;
  go_live_at: string | null;
  trial: TrialInfo | null;
  demo_products_removed: number;
}

export interface SimButton {
  id: string;
  title: string;
}

export interface SimListRow {
  id: string;
  title: string;
  description?: string | null;
}

export interface SimBubble {
  kind: "text" | "image";
  text: string | null;
  image_url: string | null;
  buttons: SimButton[];
  list_header: string | null;
  list_button_text: string | null;
  list_rows: SimListRow[];
}

export interface SimReply {
  bubbles: SimBubble[];
  stage: string;
  language: string | null;
  response_time_ms: number;
  test_passed: boolean;
  /** False while the AI model is down: replies are outage templates and do not count toward the test. */
  llm_available: boolean;
  silent: boolean;
}

export interface SimTurn {
  role: "customer" | "bot";
  text: string;
  created_at: string | null;
}

export interface SimSession {
  messages: SimTurn[];
  stage: string | null;
  test_passed: boolean;
  llm_available: boolean;
  turns_required: number;
  router_enabled: boolean;
  sample_products: number;
  daily_limit: number;
  sent_today: number;
}
