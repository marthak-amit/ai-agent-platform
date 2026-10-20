import { useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Loader2 } from "lucide-react";
import { quickStart } from "../api/client";
import Logo from "../components/Logo";
import { useAuth } from "../context/AuthContext";

type Lang = "en" | "hi" | "gu";
const LANGS: { code: Lang; label: string }[] = [
  { code: "en", label: "English" },
  { code: "hi", label: "हिन्दी" },
  { code: "gu", label: "ગુજરાતી" },
];

/**
 * The single screen shown after the first login: business name + dashboard language, then straight to the dashboard.
 * This replaces the old forced multi-step wizard; everything else is a checklist item on the dashboard.
 */
export default function QuickStart() {
  const { t, i18n } = useTranslation();
  const { client, refreshProfile } = useAuth();
  const navigate = useNavigate();
  const [name, setName] = useState(client?.business_name ?? "");
  const [lang, setLang] = useState<Lang>(
    (["en", "hi", "gu"].includes(client?.dashboard_language ?? "") ? client?.dashboard_language : i18n.language) as Lang,
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  // Only the Owner configures the business; anyone who already did this goes straight to the dashboard.
  if (client && (client.quick_start_completed || !client.current_user.is_owner)) {
    return <Navigate to="/dashboard" replace />;
  }

  function pickLanguage(next: Lang) {
    setLang(next);
    void i18n.changeLanguage(next); // preview the choice immediately
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setSaving(true);
    setError("");
    try {
      await quickStart(name.trim(), lang);
      localStorage.setItem("lang", lang);
      await refreshProfile();
      navigate("/dashboard", { replace: true });
    } catch {
      setError(t("quickstart.error"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-brand-primary/5 via-white to-brand-primary/5 flex items-center justify-center px-4 py-12">
      <form onSubmit={submit} className="bg-white rounded-2xl shadow-lg border border-gray-100 w-full max-w-md p-8 flex flex-col gap-5">
        <Logo className="h-10 w-auto self-start" />
        <div>
          <h1 className="text-2xl font-bold text-gray-900">{t("quickstart.title")}</h1>
          <p className="text-sm text-gray-500 mt-1">{t("quickstart.subtitle")}</p>
        </div>

        <div>
          <label htmlFor="qs-name" className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
            {t("quickstart.business_name")}
          </label>
          <input
            id="qs-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={120}
            required
            autoFocus
            placeholder={t("quickstart.business_placeholder")}
            className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent"
          />
        </div>

        <fieldset>
          <legend className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
            {t("quickstart.language")}
          </legend>
          <div className="grid grid-cols-3 gap-2" role="radiogroup">
            {LANGS.map((l) => (
              <button
                key={l.code}
                type="button"
                role="radio"
                aria-checked={lang === l.code}
                onClick={() => pickLanguage(l.code)}
                className={`py-2.5 rounded-lg text-sm font-medium border transition-colors ${
                  lang === l.code
                    ? "bg-brand-primaryDark text-white border-brand-primaryDark"
                    : "bg-white text-gray-700 border-gray-200 hover:border-brand-primary"
                }`}
              >
                {l.label}
              </button>
            ))}
          </div>
        </fieldset>

        <p className="text-xs text-gray-500 bg-gray-50 rounded-lg px-3 py-2">{t("quickstart.samples_note")}</p>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}

        <button
          type="submit"
          disabled={saving || !name.trim()}
          className="inline-flex items-center justify-center gap-2 bg-brand-primaryDark text-white rounded-lg py-2.5 text-sm font-semibold hover:opacity-90 disabled:opacity-50"
        >
          {saving && <Loader2 size={15} className="animate-spin" />}
          {saving ? t("quickstart.saving") : t("quickstart.continue")}
        </button>
      </form>
    </div>
  );
}
