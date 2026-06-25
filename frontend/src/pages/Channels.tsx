import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import Layout from "../components/Layout";
import AgentCanvas, { type ChannelCardConfig } from "../components/channels/AgentCanvas";
import ChannelDrawer, { CopyButton, CopyField, FieldInput, Step } from "../components/channels/ChannelDrawer";
import { disconnectInstagram, getInstagramConnectUrl, getProfile, testWhatsApp, updateChannelCredentials } from "../api/client";
import { useAuth } from "../context/AuthContext";
import { X } from "lucide-react";

const BASE_URL = (import.meta.env.VITE_API_URL ?? "https://yourplatform.com/api").replace(/\/api$/, "");

interface Profile {
  whatsapp_phone_number_id?: string;
  whatsapp_connected?: boolean;
  instagram_account_id?: string;
  instagram_connected?: boolean;
  api_key?: string;
}

function WidgetPreviewModal({ apiKey, onClose }: { apiKey: string; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md p-6 m-4">
        <div className="flex justify-between items-center mb-5">
          <h3 className="font-bold text-gray-900">Widget preview</h3>
          <button onClick={onClose} className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-gray-100 text-gray-400 transition-colors">
            <X size={18} />
          </button>
        </div>
        <div className="bg-gray-50 rounded-xl border border-gray-200 p-4 min-h-48 relative">
          <p className="text-xs text-gray-400 text-center pt-6">Your chat widget will appear as a button in the bottom-right corner of your website.</p>
          <div className="absolute bottom-4 right-4 w-12 h-12 rounded-full bg-indigo-600 flex items-center justify-center shadow-lg cursor-pointer">
            <svg className="w-6 h-6 text-white" fill="currentColor" viewBox="0 0 24 24">
              <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z" />
            </svg>
          </div>
        </div>
        <p className="text-xs text-gray-400 mt-3">API key: <span className="font-mono">{apiKey || "not generated yet"}</span></p>
        <button onClick={onClose} className="mt-4 w-full py-2.5 rounded-lg bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700 transition-colors">
          Close
        </button>
      </div>
    </div>
  );
}

const WhatsAppIcon = (
  <div className="w-10 h-10 rounded-xl bg-green-500 flex items-center justify-center">
    <svg className="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 24 24">
      <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347z" />
      <path d="M11.999 2C6.477 2 2 6.477 2 12c0 1.89.525 3.66 1.438 5.168L2 22l4.932-1.41A9.956 9.956 0 0012 22c5.523 0 10-4.477 10-10S17.523 2 12 2zm0 18c-1.66 0-3.208-.46-4.532-1.257l-.324-.192-3.367.964.983-3.288-.21-.337A7.958 7.958 0 014 12c0-4.418 3.582-8 8-8s8 3.582 8 8-3.582 8-8 8z" />
    </svg>
  </div>
);

const InstagramIcon = (
  <div
    className="w-10 h-10 rounded-xl flex items-center justify-center"
    style={{ background: "radial-gradient(circle at 30% 107%, #fdf497 0%, #fdf497 5%, #fd5949 45%, #d6249f 60%, #285AEB 90%)" }}
  >
    <svg className="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 24 24">
      <path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zM12 0C8.741 0 8.333.014 7.053.072 2.695.272.273 2.69.073 7.052.014 8.333 0 8.741 0 12c0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98C8.333 23.986 8.741 24 12 24c3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98C15.668.014 15.259 0 12 0zm0 5.838a6.162 6.162 0 100 12.324 6.162 6.162 0 000-12.324zM12 16a4 4 0 110-8 4 4 0 010 8zm6.406-11.845a1.44 1.44 0 100 2.881 1.44 1.44 0 000-2.881z" />
    </svg>
  </div>
);

const WebsiteIcon = (
  <div className="w-10 h-10 rounded-xl bg-indigo-600 flex items-center justify-center">
    <svg className="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 24 24">
      <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z" />
    </svg>
  </div>
);

type DrawerKey = "whatsapp" | "instagram" | "website" | null;

export default function Channels() {
  const { t } = useTranslation();
  const { client } = useAuth();
  const [profile, setProfile] = useState<Profile>({});
  const [loadingProfile, setLoadingProfile] = useState(true);
  const [openDrawer, setOpenDrawer] = useState<DrawerKey>(null);

  const [waPhoneId, setWaPhoneId] = useState("");
  const [waToken, setWaToken] = useState("");
  const [waSaving, setWaSaving] = useState(false);
  const [waSaved, setWaSaved] = useState(false);
  const [waTesting, setWaTesting] = useState(false);
  const [waTestResult, setWaTestResult] = useState<{ ok: boolean; msg: string } | null>(null);

  const [igConnecting, setIgConnecting] = useState(false);
  const [igDisconnecting, setIgDisconnecting] = useState(false);
  const [igStatus, setIgStatus] = useState<string | null>(null);

  const [showWidgetPreview, setShowWidgetPreview] = useState(false);

  const verifyToken = useRef(
    Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2)
  ).current;

  useEffect(() => {
    getProfile().then((p: Profile) => {
      setProfile(p);
      setWaPhoneId(p.whatsapp_phone_number_id ?? "");
      setLoadingProfile(false);
    });

    // The OAuth callback redirects back here with ?ig_status=connected|cancelled|error|no_ig_account
    const params = new URLSearchParams(window.location.search);
    const status = params.get("ig_status");
    if (status) {
      setIgStatus(status);
      setOpenDrawer("instagram");
      params.delete("ig_status");
      const rest = params.toString();
      window.history.replaceState({}, "", window.location.pathname + (rest ? `?${rest}` : ""));
      if (status === "connected") {
        getProfile().then((p: Profile) => setProfile(p));
      }
    }
  }, []);

  const waConnected = !!(profile.whatsapp_phone_number_id && profile.whatsapp_connected);
  const igConnected = !!profile.instagram_connected;
  const embedCode = `<script\n  src="${BASE_URL}/widget.js"\n  data-api-key="${profile.api_key ?? "vp_xxxxx"}">\n</script>`;

  async function refreshChannelStatus() {
    const p = await getProfile();
    setProfile(p);
  }

  async function saveWhatsApp() {
    setWaSaving(true);
    setWaTestResult(null);
    try {
      await updateChannelCredentials({ whatsapp_phone_number_id: waPhoneId, whatsapp_access_token: waToken });
      await refreshChannelStatus();
      setWaSaved(true);
      setTimeout(() => setWaSaved(false), 2000);
    } catch { /* retry */ }
    finally { setWaSaving(false); }
  }

  async function runWhatsAppTest() {
    setWaTesting(true);
    setWaTestResult(null);
    try {
      const res = await testWhatsApp();
      setWaTestResult({ ok: true, msg: res.message });
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Test failed. Check your credentials.";
      setWaTestResult({ ok: false, msg: detail });
    } finally {
      setWaTesting(false);
    }
  }

  async function connectInstagram() {
    setIgConnecting(true);
    try {
      const { url } = await getInstagramConnectUrl();
      window.location.href = url;
    } catch {
      setIgConnecting(false);
    }
  }

  async function handleDisconnectInstagram() {
    setIgDisconnecting(true);
    try {
      await disconnectInstagram();
      await refreshChannelStatus();
    } finally {
      setIgDisconnecting(false);
    }
  }

  const channelConfigs: ChannelCardConfig[] = [
    {
      id: "whatsapp",
      name: "WhatsApp",
      icon: WhatsAppIcon,
      status: loadingProfile ? "loading" : waConnected ? "connected" : "not_connected",
      onConnect: () => setOpenDrawer("whatsapp"),
      onManage: () => setOpenDrawer("whatsapp"),
    },
    {
      id: "instagram",
      name: "Instagram",
      icon: InstagramIcon,
      status: loadingProfile ? "loading" : igConnected ? "connected" : "not_connected",
      onConnect: () => setOpenDrawer("instagram"),
      onManage: () => setOpenDrawer("instagram"),
    },
    {
      id: "website",
      name: "Website",
      icon: WebsiteIcon,
      status: loadingProfile ? "loading" : "connected",
      onConnect: () => setOpenDrawer("website"),
      onManage: () => setOpenDrawer("website"),
    },
  ];

  return (
    <Layout>
      {showWidgetPreview && (
        <WidgetPreviewModal apiKey={profile.api_key ?? ""} onClose={() => setShowWidgetPreview(false)} />
      )}

      <div className="max-w-5xl mx-auto">
        <div className="mb-8">
          <h1 className="text-2xl font-bold text-gray-900">{t("channels.title")}</h1>
          <p className="text-gray-400 text-sm mt-1">{t("channels.subtitle")}</p>
        </div>

        <AgentCanvas agentName={client?.business_name ?? "Your AI Agent"} channels={channelConfigs} />
      </div>

      <ChannelDrawer title="WhatsApp" open={openDrawer === "whatsapp"} onClose={() => setOpenDrawer(null)}>
        <div className="space-y-4">
          <div className="space-y-2">
            <Step n={1} text="Get your WhatsApp Business number" />
            <Step n={2} text="Enter your Meta Phone Number ID below" />
            <Step n={3} text="Enter your WhatsApp Access Token below" />
            <Step n={4} text="Set the webhook URL in Meta Developer Dashboard → Webhooks" />
          </div>

          <CopyField label="Webhook URL" value={`${BASE_URL}/webhook`} />
          <CopyField label="Webhook Verify Token" value={verifyToken} hint="paste in Meta Dashboard" />

          <FieldInput label="Phone Number ID" value={waPhoneId} onChange={setWaPhoneId} placeholder="e.g. 123456789012345" />
          <FieldInput label="Access Token" value={waToken} onChange={setWaToken} type="password" secret placeholder="EAAxxxxxxxx…" />

          {waTestResult && (
            <div className={`rounded-lg px-4 py-2.5 text-sm ${waTestResult.ok ? "bg-green-50 text-green-700 border border-green-200" : "bg-red-50 text-red-600 border border-red-200"}`}>
              {waTestResult.msg}
            </div>
          )}

          <div className="flex gap-3">
            <button onClick={saveWhatsApp} disabled={waSaving || !waPhoneId || !waToken}
              className="flex-1 py-2.5 rounded-lg bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors">
              {waSaving ? t("channels.saving") : waSaved ? "✓ Saved" : t("channels.save")}
            </button>
            <button onClick={runWhatsAppTest} disabled={waTesting || !waConnected}
              className="flex-1 py-2.5 rounded-lg border border-indigo-200 text-indigo-700 text-sm font-semibold hover:bg-indigo-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
              {waTesting ? t("channels.testing") : t("channels.test")}
            </button>
          </div>
        </div>
      </ChannelDrawer>

      <ChannelDrawer title="Instagram" open={openDrawer === "instagram"} onClose={() => setOpenDrawer(null)}>
        <div className="space-y-4">
          {igStatus === "cancelled" && (
            <div className="rounded-lg px-4 py-2.5 text-sm bg-amber-50 text-amber-700 border border-amber-200">
              Connection cancelled. You can try again anytime.
            </div>
          )}
          {igStatus === "no_ig_account" && (
            <div className="rounded-lg px-4 py-2.5 text-sm bg-amber-50 text-amber-700 border border-amber-200">
              That Facebook account has no Instagram Business account linked. Connect your Instagram account to a Facebook Page first, then try again.
            </div>
          )}
          {igStatus === "error" && (
            <div className="rounded-lg px-4 py-2.5 text-sm bg-red-50 text-red-600 border border-red-200">
              Something went wrong connecting Instagram. Please try again.
            </div>
          )}
          {igStatus === "connected" && (
            <div className="rounded-lg px-4 py-2.5 text-sm bg-green-50 text-green-700 border border-green-200">
              Instagram connected successfully.
            </div>
          )}

          {igConnected ? (
            <>
              <div className="space-y-2">
                <Step n={1} text="Your Instagram Business account is connected." />
                <Step n={2} text="Customer DMs will be answered automatically." />
              </div>
              <CopyField label="Instagram Account ID" value={profile.instagram_account_id ?? ""} />
              <button onClick={handleDisconnectInstagram} disabled={igDisconnecting}
                className="w-full py-2.5 rounded-lg border border-red-200 text-red-600 text-sm font-semibold hover:bg-red-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors">
                {igDisconnecting ? "Disconnecting…" : "Disconnect Instagram"}
              </button>
            </>
          ) : (
            <>
              <div className="space-y-2">
                <Step n={1} text="Click Connect Instagram below" />
                <Step n={2} text="Log in with the Facebook account that manages your Instagram Business account" />
                <Step n={3} text="Approve the requested permissions" />
                <Step n={4} text="You'll be redirected back here automatically" />
              </div>
              <button onClick={connectInstagram} disabled={igConnecting}
                className="w-full py-2.5 rounded-lg bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors">
                {igConnecting ? "Redirecting…" : "Connect Instagram"}
              </button>
            </>
          )}
        </div>
      </ChannelDrawer>

      <ChannelDrawer title="Website Widget" open={openDrawer === "website"} onClose={() => setOpenDrawer(null)}>
        <div className="space-y-4">
          <p className="text-sm text-gray-600">
            Paste the code snippet below before the <code className="bg-gray-100 px-1 rounded text-xs">{"</body>"}</code> tag on your website.
          </p>
          <div>
            <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">Embed code</label>
            <div className="relative bg-gray-900 rounded-xl p-4 font-mono text-xs text-green-400 whitespace-pre leading-relaxed overflow-x-auto">
              {embedCode}
              <div className="absolute top-3 right-3">
                <CopyButton text={embedCode} />
              </div>
            </div>
          </div>
          <button onClick={() => setShowWidgetPreview(true)}
            className="w-full py-2.5 rounded-lg border border-indigo-200 text-indigo-700 text-sm font-semibold hover:bg-indigo-50 transition-colors">
            Preview Widget
          </button>
        </div>
      </ChannelDrawer>
    </Layout>
  );
}
