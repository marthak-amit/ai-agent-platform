/** Browser notification + sound preferences for new payment screenshots (stored per browser). */

export interface NotifyPrefs {
  sound: boolean;
  browser: boolean;
}

const KEY = "st24.notify.payment";

export function loadNotifyPrefs(): NotifyPrefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return { sound: true, browser: true, ...JSON.parse(raw) };
  } catch {
    /* storage unavailable — fall through to defaults */
  }
  return { sound: true, browser: true };
}

export function saveNotifyPrefs(prefs: NotifyPrefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(prefs));
  } catch {
    /* ignore */
  }
}

/** Ask for notification permission (must be called from a user gesture). */
export async function requestBrowserPermission(): Promise<NotificationPermission | "unsupported"> {
  if (typeof Notification === "undefined") return "unsupported";
  if (Notification.permission === "granted") return "granted";
  try {
    return await Notification.requestPermission();
  } catch {
    return "denied";
  }
}

let audioCtx: AudioContext | null = null;

/** Two-tone chime via WebAudio (no asset to ship; silently no-ops if autoplay is blocked). */
export function playChime(): void {
  try {
    const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return;
    audioCtx = audioCtx ?? new Ctx();
    const ctx = audioCtx;
    if (ctx.state === "suspended") void ctx.resume();
    [660, 880].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      const t0 = ctx.currentTime + i * 0.16;
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.25, t0 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.28);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t0);
      osc.stop(t0 + 0.3);
    });
  } catch {
    /* audio blocked or unavailable */
  }
}

export function showBrowserNotification(title: string, body: string, onClick?: () => void): void {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try {
    const n = new Notification(title, { body, tag: "st24-payment", icon: "/favicon.ico" });
    n.onclick = () => {
      window.focus();
      onClick?.();
      n.close();
    };
  } catch {
    /* some mobile browsers require a service worker — ignore */
  }
}
