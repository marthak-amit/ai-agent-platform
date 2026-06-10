import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { login, register } from "../api/client";
import { useAuth } from "../context/AuthContext";
import { Zap, CheckCircle2 } from "lucide-react";

type Mode = "login" | "register";

export default function Login() {
  const { t } = useTranslation();
  const { signIn } = useAuth();
  const navigate = useNavigate();

  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [businessName, setBusinessName] = useState("");
  const [phone, setPhone] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      if (mode === "register") {
        await register(email, password, businessName, phone || undefined);
        const token = await login(email, password);
        await signIn(token);
        navigate("/onboarding");
      } else {
        const token = await login(email, password);
        await signIn(token);
        navigate("/dashboard");
      }
    } catch (err: unknown) {
      if (
        err &&
        typeof err === "object" &&
        "response" in err &&
        (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      ) {
        setError((err as { response: { data: { detail: string } } }).response.data.detail);
      } else {
        setError(t("auth.invalid"));
      }
    } finally {
      setLoading(false);
    }
  }

  function switchMode(next: Mode) {
    setMode(next);
    setError("");
  }

  const features = [
    "WhatsApp + Instagram automation",
    "Hindi, Gujarati & English support",
    "Real-time analytics dashboard",
  ];

  return (
    <div className="min-h-screen flex font-sans">
      {/* Left panel — indigo gradient */}
      <div className="hidden lg:flex flex-col justify-between w-[480px] shrink-0 bg-gradient-to-br from-indigo-600 to-indigo-800 p-12">
        <div>
          <div className="flex items-center gap-3 mb-12">
            <div className="w-10 h-10 bg-white/20 rounded-xl flex items-center justify-center">
              <Zap size={20} className="text-white" />
            </div>
            <span className="font-bold text-white text-xl">AgentlyAI</span>
          </div>

          <h2 className="text-3xl font-bold text-white leading-tight mb-4">
            AI-powered sales agent for Indian businesses
          </h2>
          <p className="text-indigo-200 text-base mb-10">
            Automate your WhatsApp & Instagram customer conversations. Capture leads, answer queries, and grow sales — 24/7.
          </p>

          <div className="space-y-4">
            {features.map((f) => (
              <div key={f} className="flex items-center gap-3">
                <CheckCircle2 size={18} className="text-indigo-300 shrink-0" />
                <span className="text-white text-sm">{f}</span>
              </div>
            ))}
          </div>
        </div>

        <p className="text-indigo-300 text-xs">© 2025 AgentlyAI. Made in India 🇮🇳</p>
      </div>

      {/* Right panel — white */}
      <div className="flex-1 flex flex-col items-center justify-center px-6 py-12 bg-white">
        <div className="w-full max-w-sm">
          {/* Mobile logo */}
          <div className="flex items-center gap-2 mb-8 lg:hidden">
            <div className="w-8 h-8 bg-indigo-600 rounded-lg flex items-center justify-center">
              <Zap size={16} className="text-white" />
            </div>
            <span className="font-bold text-gray-900">AgentlyAI</span>
          </div>

          <h1 className="text-2xl font-bold text-gray-900 mb-1">
            {mode === "login" ? "Welcome back" : "Create your account"}
          </h1>
          <p className="text-sm text-gray-500 mb-8">
            {mode === "login"
              ? "Sign in to your AgentlyAI dashboard"
              : "Set up your AI sales agent in minutes"}
          </p>

          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            {mode === "register" && (
              <div>
                <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
                  {t("auth.business_name")}
                </label>
                <input
                  type="text"
                  value={businessName}
                  onChange={(e) => setBusinessName(e.target.value)}
                  required
                  className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all"
                  placeholder="Raj's Electronics"
                />
              </div>
            )}

            <div>
              <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
                {t("auth.email")}
              </label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all"
                placeholder="you@business.com"
              />
            </div>

            <div>
              <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
                {t("auth.password")}
              </label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={8}
                className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all"
              />
            </div>

            {mode === "register" && (
              <div>
                <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
                  {t("auth.phone")}{" "}
                  <span className="text-gray-300 normal-case font-normal">{t("auth.phone_optional")}</span>
                </label>
                <input
                  type="tel"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all"
                  placeholder="+91 98765 43210"
                />
              </div>
            )}

            {error && (
              <div className="bg-red-50 border border-red-100 rounded-lg px-4 py-2.5">
                <p className="text-sm text-red-600">{error}</p>
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full bg-indigo-600 text-white rounded-lg py-2.5 text-sm font-semibold hover:bg-indigo-700 active:scale-95 disabled:opacity-50 transition-all duration-150 mt-1"
            >
              {loading
                ? mode === "register" ? t("auth.creating") : t("auth.signing_in")
                : mode === "register" ? t("auth.create_account") : t("auth.sign_in")}
            </button>
          </form>

          <p className="text-sm text-center text-gray-400 mt-6">
            {mode === "login" ? (
              <>
                {t("auth.no_account")}{" "}
                <button
                  onClick={() => switchMode("register")}
                  className="text-indigo-600 font-medium hover:underline"
                >
                  {t("auth.sign_up")}
                </button>
              </>
            ) : (
              <>
                {t("auth.have_account")}{" "}
                <button
                  onClick={() => switchMode("login")}
                  className="text-indigo-600 font-medium hover:underline"
                >
                  {t("auth.sign_in")}
                </button>
              </>
            )}
          </p>

          <p className="text-center text-xs text-gray-300 mt-8">Powered by AgentlyAI</p>
        </div>
      </div>
    </div>
  );
}
