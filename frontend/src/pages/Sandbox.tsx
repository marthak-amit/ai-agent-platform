import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { RotateCcw, Send, Copy, FlaskConical, AlertTriangle } from "lucide-react";
import Layout from "../components/Layout";

interface SandboxMessage {
  role: "user" | "agent";
  text: string;
  language?: string;
  stage?: string;
  leadStatus?: string;
  products?: string[];
  responseMs?: number;
}

interface SandboxResponse {
  reply: string;
  language_detected: string;
  stage: string;
  lead_status: string;
  products_matched: string[];
  response_time_ms: number;
}

const PRESET_MESSAGES = [
  "Hello",
  "Show me sarees",
  "What's in stock?",
  "I want to order",
  "What sizes?",
];

const LEAD_COLORS: Record<string, string> = {
  hot: "bg-red-100 text-red-700",
  warm: "bg-amber-100 text-amber-700",
  cold: "bg-blue-100 text-blue-700",
};

export function SandboxUI() {
  const [messages, setMessages] = useState<SandboxMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function sendMessage(text?: string) {
    const msg = text ?? input.trim();
    if (!msg || loading) return;
    setInput("");
    setMessages((prev) => [...prev, { role: "user", text: msg }]);
    setLoading(true);
    try {
      const { data } = await api.post<SandboxResponse>("/sandbox/message", {
        message: msg,
        phone: "test_user",
      });
      setMessages((prev) => [
        ...prev,
        {
          role: "agent",
          text: data.reply,
          language: data.language_detected,
          stage: data.stage,
          leadStatus: data.lead_status,
          products: data.products_matched,
          responseMs: data.response_time_ms,
        },
      ]);
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: "agent", text: "Error — could not reach the agent. Please try again." },
      ]);
    } finally {
      setLoading(false);
    }
  }

  async function handleReset() {
    await api.get("/sandbox/reset");
    setMessages([]);
  }

  function handleCopy() {
    const text = messages
      .map((m) => `${m.role === "user" ? "You" : "Agent"}: ${m.text}`)
      .join("\n");
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  }

  return (
    <div className="max-w-2xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <FlaskConical size={20} className="text-indigo-600" />
          <div>
            <h1 className="text-xl font-bold text-gray-900">Test Your Agent</h1>
            <p className="text-xs text-gray-400">Chat with your agent before going live</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleCopy}
            className="flex items-center gap-1.5 text-xs border border-gray-200 rounded-lg px-3 py-1.5 text-gray-600 hover:bg-gray-50 transition-colors"
          >
            <Copy size={13} />
            {copied ? "Copied!" : "Copy Chat"}
          </button>
          <button
            onClick={handleReset}
            className="flex items-center gap-1.5 text-xs border border-gray-200 rounded-lg px-3 py-1.5 text-gray-600 hover:bg-gray-50 transition-colors"
          >
            <RotateCcw size={13} />
            Reset Chat
          </button>
        </div>
      </div>

      {/* Warning banner */}
      <div className="flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-xl px-4 py-3 mb-4 text-sm text-amber-800">
        <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-500" />
        <span>
          This is a test environment. Messages here are <strong>NOT</strong> sent to real customers.
        </span>
      </div>

      {/* Chat window */}
      <div className="bg-[#ECE5DD] rounded-xl border border-gray-200 h-[400px] overflow-y-auto p-4 flex flex-col gap-3">
        {messages.length === 0 && (
          <div className="flex-1 flex items-center justify-center">
            <p className="text-sm text-gray-400">Send a message to start testing your agent.</p>
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={`flex flex-col ${m.role === "user" ? "items-end" : "items-start"}`}>
            {m.role === "user" && (
              <span className="text-[10px] text-gray-500 mb-0.5 mr-1">Test User</span>
            )}
            {m.role === "agent" && (
              <span className="text-[10px] text-gray-500 mb-0.5 ml-1">Agent</span>
            )}

            {/* Bubble */}
            <div
              className={`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm whitespace-pre-wrap shadow-sm ${
                m.role === "user"
                  ? "bg-[#DCF8C6] text-gray-900 rounded-tr-sm"
                  : "bg-white text-gray-900 rounded-tl-sm"
              }`}
            >
              {m.text}
            </div>

            {/* Metadata pills — only on agent messages */}
            {m.role === "agent" && (m.language || m.stage || m.leadStatus) && (
              <div className="flex flex-wrap gap-1 mt-1 ml-1">
                {m.language && (
                  <span className="text-[10px] bg-gray-100 text-gray-500 rounded-full px-2 py-0.5">
                    🌐 {m.language}
                  </span>
                )}
                {m.leadStatus && (
                  <span className={`text-[10px] rounded-full px-2 py-0.5 ${LEAD_COLORS[m.leadStatus] ?? "bg-gray-100 text-gray-500"}`}>
                    📊 {m.leadStatus} lead
                  </span>
                )}
                {m.stage && (
                  <span className="text-[10px] bg-indigo-50 text-indigo-600 rounded-full px-2 py-0.5">
                    🔍 {m.stage.replace(/_/g, " ")}
                  </span>
                )}
                {m.products && m.products.length > 0 && (
                  <span className="text-[10px] bg-emerald-50 text-emerald-600 rounded-full px-2 py-0.5">
                    🛍 {m.products.join(", ")}
                  </span>
                )}
                {m.responseMs !== undefined && (
                  <span className="text-[10px] bg-gray-50 text-gray-400 rounded-full px-2 py-0.5">
                    ⚡ {m.responseMs}ms
                  </span>
                )}
              </div>
            )}
          </div>
        ))}

        {loading && (
          <div className="flex items-start">
            <div className="bg-white rounded-2xl rounded-tl-sm px-4 py-2.5 shadow-sm">
              <div className="flex gap-1">
                <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:0ms]" />
                <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:150ms]" />
                <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:300ms]" />
              </div>
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Preset quick-buttons */}
      <div className="flex flex-wrap gap-2 mt-3">
        {PRESET_MESSAGES.map((p) => (
          <button
            key={p}
            onClick={() => sendMessage(p)}
            disabled={loading}
            className="text-xs border border-gray-200 rounded-full px-3 py-1 text-gray-600 hover:bg-gray-50 disabled:opacity-40 transition-colors"
          >
            {p}
          </button>
        ))}
      </div>

      {/* Input bar */}
      <div className="flex gap-2 mt-3">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={loading}
          placeholder="Type a message…"
          className="flex-1 border border-gray-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:bg-gray-50"
        />
        <button
          onClick={() => sendMessage()}
          disabled={loading || !input.trim()}
          className="bg-indigo-600 text-white rounded-xl px-4 py-2.5 hover:bg-indigo-700 disabled:opacity-40 transition-colors"
        >
          <Send size={16} />
        </button>
      </div>
    </div>
  );
}

export default function SandboxPage() {
  return (
    <Layout>
      <SandboxUI />
    </Layout>
  );
}
