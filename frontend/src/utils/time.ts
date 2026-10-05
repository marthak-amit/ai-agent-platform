/** Small time helpers shared by the inbox and the payments page. */

export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const mins = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 60_000));
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.floor(hrs / 24)}d`;
}

export function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true });
}

export function dayLabel(iso: string): string {
  const d = new Date(iso);
  const today = new Date();
  const yesterday = new Date(Date.now() - 86_400_000);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: true });
}

/** "5h 20m" / "12m" / "45s" from a number of seconds. */
export function durationShort(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${s}s`;
}

export function rupees(n: number): string {
  return `₹${Math.round(n).toLocaleString("en-IN")}`;
}

/** Seconds → "12 min ago" style label used for payment waiting time. */
export function waitingLabel(seconds: number | null | undefined, t: (k: string, o?: Record<string, unknown>) => string): string {
  const s = seconds ?? 0;
  if (s < 60) return t("payments.just_now");
  if (s < 3600) return t("payments.min_ago", { n: Math.floor(s / 60) });
  if (s < 86_400) return t("payments.hr_ago", { n: Math.floor(s / 3600) });
  return t("payments.day_ago", { n: Math.floor(s / 86_400) });
}
