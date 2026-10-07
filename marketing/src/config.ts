// Base URL of the dashboard SPA (frontend/) — a separate app from this
// marketing site. Set VITE_DASHBOARD_URL in marketing/.env (dev server URL
// locally, real domain in prod).
export const DASHBOARD_URL: string = import.meta.env.VITE_DASHBOARD_URL ?? "";

export const DASHBOARD_LOGIN_URL = `${DASHBOARD_URL}/login`;
// The dashboard's /login screen opens in "create account" mode when `mode=register` is present and
// remembers `plan=<billing_plans.code>` so /billing and the onboarding plan step can preselect it.
export const DASHBOARD_SIGNUP_URL = `${DASHBOARD_URL}/login?mode=register`;

/** Signup link that carries the chosen plan, e.g. dashboardSignupUrl("growth_5000"). */
export function dashboardSignupUrl(planCode: string): string {
  return `${DASHBOARD_SIGNUP_URL}&plan=${encodeURIComponent(planCode)}`;
}

export const CALENDLY_URL: string =
  import.meta.env.VITE_CALENDLY_URL ?? "https://calendly.com/amit-marthak03/30min";
