import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { CheckCircle2, FlaskConical, ListOrdered, Loader2, RotateCcw, Send } from "lucide-react";
import { getSimulatorSession, resetSimulator, sendSimulatorMessage } from "../api/client";
import Layout from "../components/Layout";
import { useSetup } from "../context/SetupContext";
import type { SimBubble, SimReply, SimSession } from "../types/setup";

/** One rendered chat line: the owner playing the customer, or a bubble the bot sent. */
type Line =
  | { id: number; role: "customer"; text: string }
  | { id: number; role: "bot"; bubble: SimBubble; live: boolean };

let lineId = 0;

function errorMessage(err: unknown, fallback: string, limit: string): string {
  const res = (err as { response?: { status?: number; data?: { detail?: unknown } } })?.response;
  if (res?.status === 429) return limit;
  return typeof res?.data?.detail === "string" ? res.data.detail : fallback;
}

/**
 * The in-dashboard chat simulator: the owner plays a customer against the REAL agent (the same router and order
 * engine as WhatsApp, on the real catalogue). Nothing is sent to WhatsApp, and nothing here counts toward usage or
 * the trial, so it is free and unlimited before — and after — going live.
 */
export default function SandboxPage() {
  const { t } = useTranslation();
  const { refresh: refreshSetup, checklist } = useSetup();
  const [session, setSession] = useState<SimSession | null>(null);
  const [lines, setLines] = useState<Line[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [error, setError] = useState("");
  const [passed, setPassed] = useState(false);
  const [llmUp, setLlmUp] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Resume the stored transcript after a reload (text only — buttons are only live on the latest reply).
  useEffect(() => {
    let cancelled = false;
    getSimulatorSession()
      .then((s) => {
        if (cancelled) return;
        setSession(s);
        setPassed(s.test_passed);
        setLlmUp(s.llm_available);
        setLines(
          s.messages.map((m): Line =>
            m.role === "customer"
              ? { id: ++lineId, role: "customer", text: m.text }
              : {
                  id: ++lineId,
                  role: "bot",
                  live: false,
                  bubble: { kind: "text", text: m.text, image_url: null, buttons: [], list_header: null, list_button_text: null, list_rows: [] },
                },
          ),
        );
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [lines, busy]);

  const apply = useCallback(
    async (reply: SimReply) => {
      setLines((prev) => {
        // Buttons/lists on earlier replies are stale (like WhatsApp after a newer card): freeze them.
        const frozen = prev.map((l) => (l.role === "bot" && l.live ? { ...l, live: false } : l));
        return [...frozen, ...reply.bubbles.map((bubble): Line => ({ id: ++lineId, role: "bot", bubble, live: true }))];
      });
      setLlmUp(reply.llm_available);
      if (reply.test_passed && !passed) {
        setPassed(true);
        void refreshSetup();
      }
    },
    [passed, refreshSetup],
  );

  async function send(body: Parameters<typeof sendSimulatorMessage>[0], shown: string) {
    if (busy) return;
    setError("");
    setBusy(true);
    setLines((prev) => [...prev, { id: ++lineId, role: "customer", text: shown }]);
    try {
      await apply(await sendSimulatorMessage(body));
      setSession((s) => (s ? { ...s, sent_today: s.sent_today + 1 } : s));
    } catch (err) {
      setError(errorMessage(err, t("simulator.error"), t("simulator.limit")));
    } finally {
      setBusy(false);
    }
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text) return;
    setInput("");
    void send({ text }, text);
  }

  async function reset() {
    setResetting(true);
    setError("");
    try {
      await resetSimulator();
      setLines([]);
    } catch {
      setError(t("simulator.error"));
    } finally {
      setResetting(false);
    }
  }

  const userTurns = lines.filter((l) => l.role === "customer").length;
  const required = session?.turns_required ?? 3;
  const samples = checklist?.demo_products ?? session?.sample_products ?? 0;
  const suggestions = [t("simulator.suggest_hi"), t("simulator.suggest_products"), t("simulator.suggest_order")];

  return (
    <Layout>
      <div className="max-w-2xl mx-auto flex flex-col gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-900 flex items-center gap-2">
            <FlaskConical size={20} className="text-purple-600" />
            {t("simulator.title")}
          </h1>
          <p className="text-sm text-gray-500 mt-1">{t("simulator.subtitle")}</p>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs">
          {session && (
            <span className="px-2.5 py-1 rounded-full bg-gray-100 text-gray-600">
              {session.router_enabled ? t("simulator.router_on") : t("simulator.router_off")}
            </span>
          )}
          {samples > 0 && (
            <span className="px-2.5 py-1 rounded-full bg-amber-100 text-amber-800">{t("simulator.samples_banner", { count: samples })}</span>
          )}
          {passed ? (
            <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full bg-emerald-100 text-emerald-700 font-medium">
              <CheckCircle2 size={13} /> {t("simulator.passed")}
            </span>
          ) : (
            <span className="px-2.5 py-1 rounded-full bg-blue-50 text-blue-700">
              {t("simulator.progress", { done: Math.min(userTurns, required), total: required })}
            </span>
          )}
          <button
            type="button"
            onClick={reset}
            disabled={resetting || busy || lines.length === 0}
            className="ml-auto inline-flex items-center gap-1.5 text-gray-500 hover:text-gray-800 disabled:opacity-40"
          >
            {resetting ? <Loader2 size={13} className="animate-spin" /> : <RotateCcw size={13} />}
            {resetting ? t("simulator.resetting") : t("simulator.reset")}
          </button>
        </div>

        {/* Chat */}
        <div className="bg-[#ECE5DD] rounded-2xl p-3 sm:p-4 h-[56vh] min-h-[320px] overflow-y-auto flex flex-col gap-2" aria-live="polite">
          {lines.length === 0 && !busy && (
            <div className="m-auto text-center text-sm text-gray-500 max-w-xs">
              <p>{t("simulator.empty")}</p>
              <div className="flex flex-wrap justify-center gap-2 mt-3">
                <span className="text-xs text-gray-400 self-center">{t("simulator.try")}</span>
                {suggestions.map((sg) => (
                  <button
                    key={sg}
                    type="button"
                    onClick={() => void send({ text: sg }, sg)}
                    className="text-xs bg-white border border-gray-200 rounded-full px-3 py-1 text-gray-700 hover:bg-gray-50"
                  >
                    {sg}
                  </button>
                ))}
              </div>
            </div>
          )}

          {lines.map((l) =>
            l.role === "customer" ? (
              <div key={l.id} className="flex justify-end">
                <div className="bg-[#DCF8C6] rounded-2xl rounded-tr-sm px-3 py-2 text-sm shadow-sm max-w-[80%] whitespace-pre-wrap break-words">{l.text}</div>
              </div>
            ) : (
              <BotBubble key={l.id} bubble={l.bubble} interactive={l.live && !busy} onTap={(body, shown) => void send(body, shown)} />
            ),
          )}

          {busy && (
            <div className="flex justify-start" aria-label={t("simulator.typing")}>
              <div className="bg-white rounded-2xl rounded-tl-sm px-4 py-2.5 shadow-sm flex gap-1">
                <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:0ms]" />
                <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:150ms]" />
                <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:300ms]" />
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        {!llmUp && !passed && (
          <p role="status" className="text-sm text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
            {t("simulator.llm_down")}
          </p>
        )}

        <form onSubmit={submit} className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={busy}
            maxLength={2000}
            placeholder={t("simulator.placeholder")}
            aria-label={t("simulator.placeholder")}
            className="flex-1 border border-gray-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand-primary disabled:bg-gray-50"
          />
          <button
            type="submit"
            disabled={busy || !input.trim()}
            aria-label={t("simulator.send")}
            className="bg-brand-primaryDark text-white rounded-xl px-4 py-2.5 hover:opacity-90 disabled:opacity-40"
          >
            <Send size={16} />
          </button>
        </form>

        {passed && (
          <p className="text-sm text-gray-600">
            {t("simulator.next_go_live")}{" "}
            <Link to="/dashboard" className="font-semibold text-brand-primaryDark underline">
              {t("nav.dashboard")}
            </Link>
          </p>
        )}
      </div>
    </Layout>
  );
}

/** A bot bubble with its quick-reply buttons / list rows (tappable only on the latest reply). */
function BotBubble({
  bubble,
  interactive,
  onTap,
}: {
  bubble: SimBubble;
  interactive: boolean;
  onTap: (body: { button_id: string; title: string } | { list_row_id: string; title: string }, shown: string) => void;
}) {
  const { t } = useTranslation();
  const [listOpen, setListOpen] = useState(false);
  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] flex flex-col gap-1">
        <div className="bg-white rounded-2xl rounded-tl-sm px-3 py-2 text-sm shadow-sm whitespace-pre-wrap break-words">
          {bubble.kind === "image" && bubble.image_url && (
            <img src={bubble.image_url} alt={bubble.text ?? t("simulator.image")} className="rounded-lg mb-1.5 max-h-52 object-cover" loading="lazy" />
          )}
          {bubble.text}
        </div>

        {bubble.buttons.length > 0 && (
          <div className="flex flex-col gap-1">
            {bubble.buttons.map((b) => (
              <button
                key={b.id}
                type="button"
                disabled={!interactive}
                onClick={() => onTap({ button_id: b.id, title: b.title }, b.title)}
                className="bg-white text-[#0b7ab8] font-medium text-sm rounded-xl py-2 shadow-sm hover:bg-gray-50 disabled:opacity-60 disabled:hover:bg-white"
              >
                {b.title}
              </button>
            ))}
          </div>
        )}

        {bubble.list_rows.length > 0 && (
          <>
            <button
              type="button"
              onClick={() => setListOpen((o) => !o)}
              className="bg-white text-[#0b7ab8] font-medium text-sm rounded-xl py-2 shadow-sm inline-flex items-center justify-center gap-1.5"
            >
              <ListOrdered size={14} /> {bubble.list_button_text || t("simulator.open_list")}
            </button>
            {listOpen && (
              <ul className="bg-white rounded-xl shadow-sm divide-y divide-gray-100 overflow-hidden">
                {bubble.list_rows.map((r) => (
                  <li key={r.id}>
                    <button
                      type="button"
                      disabled={!interactive}
                      onClick={() => {
                        setListOpen(false);
                        onTap({ list_row_id: r.id, title: r.title }, r.title);
                      }}
                      className="w-full text-left px-3 py-2 hover:bg-gray-50 disabled:opacity-60"
                    >
                      <span className="block text-sm font-medium text-gray-800">{r.title}</span>
                      {r.description && <span className="block text-xs text-gray-500">{r.description}</span>}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </div>
  );
}
