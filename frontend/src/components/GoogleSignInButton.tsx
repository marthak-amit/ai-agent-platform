import { useEffect, useRef, useState } from "react";
import { getGoogleConfig } from "../api/client";

/** The slice of Google Identity Services we use (https://developers.google.com/identity/gsi/web/reference/js-reference). */
interface GoogleId {
  initialize(config: {
    client_id: string;
    callback: (response: { credential?: string }) => void;
    ux_mode?: "popup" | "redirect";
    auto_select?: boolean;
    use_fedcm_for_prompt?: boolean;
  }): void;
  renderButton(
    parent: HTMLElement,
    options: {
      type?: "standard" | "icon";
      theme?: "outline" | "filled_blue" | "filled_black";
      size?: "large" | "medium" | "small";
      text?: "signin_with" | "signup_with" | "continue_with" | "signin";
      shape?: "rectangular" | "pill";
      width?: number;
      locale?: string;
    },
  ): void;
}

declare global {
  interface Window {
    google?: { accounts: { id: GoogleId } };
  }
}

const GSI_SRC = "https://accounts.google.com/gsi/client";
let scriptPromise: Promise<void> | null = null;

/** Load Google's sign-in script once. Rejects (and allows a retry) when it cannot be loaded. */
function loadGsi(): Promise<void> {
  if (window.google?.accounts?.id) return Promise.resolve();
  if (!scriptPromise) {
    scriptPromise = new Promise<void>((resolve, reject) => {
      const el = document.createElement("script");
      el.src = GSI_SRC;
      el.async = true;
      el.defer = true;
      el.onload = () => resolve();
      el.onerror = () => {
        scriptPromise = null;
        el.remove();
        reject(new Error("Could not load Google sign-in"));
      };
      document.head.appendChild(el);
    });
  }
  return scriptPromise;
}

interface Props {
  /** Called with the Google ID token (`credential`); the backend verifies it. */
  onCredential: (credential: string) => void;
  /** Called once with whether the button is shown (false when not configured or the Google script is blocked). */
  onAvailabilityChange?: (available: boolean) => void;
  locale?: string;
}

/**
 * Google's own "Continue with Google" button (Google Identity Services, ID-token flow).
 *
 * The account chooser / consent screen is Google's and shows the SellerTalk24 brand configured on our Google Cloud
 * OAuth consent screen — there is no Firebase here, so no firebaseapp.com / project domain can appear. The client id
 * comes from the backend (GET /auth/google/config), so no build-time env var is needed. Renders nothing when Google
 * sign-in is not configured.
 */
export default function GoogleSignInButton({ onCredential, onAvailabilityChange, locale }: Props) {
  const holder = useRef<HTMLDivElement>(null);
  const [enabled, setEnabled] = useState(false);
  const cb = useRef(onCredential);
  cb.current = onCredential;
  const availability = useRef(onAvailabilityChange);
  availability.current = onAvailabilityChange;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cfg = await getGoogleConfig();
        if (!cfg.enabled || !cfg.client_id) {
          availability.current?.(false);
          return;
        }
        await loadGsi();
        if (cancelled || !holder.current || !window.google) return;
        window.google.accounts.id.initialize({
          client_id: cfg.client_id,
          ux_mode: "popup",
          callback: (resp) => {
            if (resp.credential) cb.current(resp.credential);
          },
        });
        window.google.accounts.id.renderButton(holder.current, {
          type: "standard",
          theme: "outline",
          size: "large",
          text: "continue_with",
          shape: "rectangular",
          width: Math.min(360, Math.max(240, holder.current.clientWidth || 320)),
          locale,
        });
        setEnabled(true);
        availability.current?.(true);
      } catch {
        availability.current?.(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [locale]);

  return <div ref={holder} className={enabled ? "flex justify-center min-h-[44px]" : "hidden"} data-testid="google-signin" />;
}
