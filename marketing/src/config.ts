// Base URL of the dashboard SPA (frontend/) — a separate app from this
// marketing site. Set VITE_DASHBOARD_URL in marketing/.env (dev server URL
// locally, real domain in prod).
export const DASHBOARD_URL: string = import.meta.env.VITE_DASHBOARD_URL ?? "";

export const DASHBOARD_LOGIN_URL = `${DASHBOARD_URL}/login`;
// frontend/src/App.tsx only registers a "/login" route; Login.tsx toggles
// login/register mode client-side with no query param support, so both CTAs
// land on the same /login screen.
export const DASHBOARD_SIGNUP_URL = `${DASHBOARD_URL}/login`;

export const CALENDLY_URL: string =
  import.meta.env.VITE_CALENDLY_URL ?? "https://calendly.com/amit-marthak03/30min";
