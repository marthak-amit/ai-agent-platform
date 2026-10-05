/**
 * WhatsApp Embedded Signup (Meta Tech Provider flow) — loads the Facebook JS
 * SDK, drives the FB.login() popup, and correlates its result with the
 * WA_EMBEDDED_SIGNUP postMessage event the popup sends before it closes.
 *
 * See https://developers.facebook.com/docs/whatsapp/embedded-signup
 */

declare global {
  interface Window {
    FB?: {
      init: (params: {
        appId: string;
        autoLogAppUser: boolean;
        xfbml: boolean;
        version: string;
      }) => void;
      login: (
        callback: (response: { authResponse?: { code?: string } }) => void,
        params: {
          config_id: string;
          response_type: string;
          override_default_response_type: boolean;
          extras: { feature: string; sessionInfoVersion: string };
        }
      ) => void;
    };
    fbAsyncInit?: () => void;
  }
}

let sdkLoadPromise: Promise<void> | null = null;

function loadFacebookSdk(appId: string): Promise<void> {
  if (window.FB) return Promise.resolve();
  if (sdkLoadPromise) return sdkLoadPromise;

  sdkLoadPromise = new Promise((resolve, reject) => {
    window.fbAsyncInit = () => {
      window.FB!.init({ appId, autoLogAppUser: false, xfbml: false, version: "v21.0" });
      resolve();
    };
    const script = document.createElement("script");
    script.src = "https://connect.facebook.net/en_US/sdk.js";
    script.async = true;
    script.defer = true;
    script.onerror = () => {
      sdkLoadPromise = null;
      reject(new Error("Failed to load the Facebook SDK. Check your connection and try again."));
    };
    document.body.appendChild(script);
  });
  return sdkLoadPromise;
}

export interface EmbeddedSignupResult {
  code: string;
  wabaId: string;
  phoneNumberId: string;
}

/**
 * Launch the Embedded Signup popup and resolve once the client has picked a
 * WhatsApp Business Account + phone number and the popup has closed.
 *
 * The popup posts a WA_EMBEDDED_SIGNUP message with the waba_id/phone_number_id
 * shortly before FB.login()'s own callback fires with the authorization code —
 * both are needed to complete the exchange server-side.
 */
export async function launchWhatsAppEmbeddedSignup(
  appId: string,
  configId: string
): Promise<EmbeddedSignupResult> {
  await loadFacebookSdk(appId);

  return new Promise((resolve, reject) => {
    let sessionInfo: { wabaId?: string; phoneNumberId?: string } = {};

    function handleMessage(event: MessageEvent) {
      if (!event.origin.endsWith("facebook.com")) return;
      let data: unknown;
      try {
        data = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
      } catch {
        return;
      }
      const payload = data as { type?: string; event?: string; data?: Record<string, string> };
      if (payload?.type !== "WA_EMBEDDED_SIGNUP") return;

      if (payload.event === "FINISH" || payload.event === "FINISH_ONLY_WABA") {
        sessionInfo = {
          wabaId: payload.data?.waba_id,
          phoneNumberId: payload.data?.phone_number_id,
        };
      } else if (payload.event === "CANCEL" || payload.event === "ERROR") {
        window.removeEventListener("message", handleMessage);
        reject(new Error(payload.data?.error_message || "WhatsApp connection was cancelled."));
      }
    }

    window.addEventListener("message", handleMessage);

    window.FB!.login(
      (response) => {
        window.removeEventListener("message", handleMessage);
        const code = response?.authResponse?.code;
        if (!code || !sessionInfo.wabaId || !sessionInfo.phoneNumberId) {
          reject(new Error("WhatsApp connection was not completed."));
          return;
        }
        resolve({ code, wabaId: sessionInfo.wabaId, phoneNumberId: sessionInfo.phoneNumberId });
      },
      {
        config_id: configId,
        response_type: "code",
        override_default_response_type: true,
        extras: { feature: "whatsapp_embedded_signup", sessionInfoVersion: "3" },
      }
    );
  });
}
