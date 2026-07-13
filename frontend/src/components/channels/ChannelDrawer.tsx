import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { X, Eye, EyeOff } from "lucide-react";

export function CopyButton({ text }: { text: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  function copy() {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  }
  return (
    <button
      onClick={copy}
      className="px-2.5 py-1 text-xs rounded-lg bg-white/10 hover:bg-white/20 text-gray-400 hover:text-gray-600 transition-colors border border-gray-200"
    >
      {copied ? t("channels.copied") : t("channels.copy")}
    </button>
  );
}

export function CopyField({ label, value, hint }: { label: string; value: string; hint?: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  function copy() {
    navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  }
  return (
    <div>
      <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
        {label} {hint && <span className="font-normal normal-case text-gray-400">({hint})</span>}
      </label>
      <div className="flex items-center bg-gray-50 border border-gray-200 rounded-lg px-3 py-2.5 font-mono text-xs text-gray-600">
        <span className="flex-1 truncate">{value}</span>
        <button
          onClick={copy}
          className="ml-2 px-2.5 py-1 text-xs rounded-lg bg-white/10 hover:bg-white/20 text-gray-400 hover:text-gray-600 transition-colors border border-gray-200"
        >
          {copied ? t("channels.copied") : t("channels.copy")}
        </button>
      </div>
    </div>
  );
}

export function FieldInput({
  label, value, onChange, type = "text", placeholder, secret = false,
}: { label: string; value: string; onChange: (v: string) => void; type?: string; placeholder?: string; secret?: boolean }) {
  const [revealed, setRevealed] = useState(false);
  const isSecretInput = secret && type === "password";
  return (
    <div>
      <label className="block text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">{label}</label>
      <div className="relative">
        <input
          type={isSecretInput && !revealed ? "password" : "text"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className="w-full border border-gray-200 rounded-lg px-3.5 py-2.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-brand-primary focus:border-transparent transition-all pr-10"
        />
        {isSecretInput && (
          <button
            type="button"
            onClick={() => setRevealed((v) => !v)}
            className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 transition-colors"
            aria-label={revealed ? "Hide token" : "Reveal token"}
          >
            {revealed ? <EyeOff size={15} /> : <Eye size={15} />}
          </button>
        )}
      </div>
    </div>
  );
}

export function Step({ n, text }: { n: number; text: string }) {
  return (
    <div className="flex items-start gap-3">
      <span className="shrink-0 w-6 h-6 rounded-full bg-brand-primary/10 text-brand-primaryDark text-xs font-bold flex items-center justify-center mt-0.5">
        {n}
      </span>
      <span className="text-sm text-gray-600 leading-relaxed">{text}</span>
    </div>
  );
}

interface ChannelDrawerProps {
  title: string;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}

export default function ChannelDrawer({ title, open, onClose, children }: ChannelDrawerProps) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />
      <div className="relative w-full max-w-md h-full bg-white shadow-xl flex flex-col animate-in">
        <div className="flex items-center justify-between px-6 py-5 border-b border-gray-100">
          <h3 className="font-bold text-gray-900">{title}</h3>
          <button onClick={onClose} className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-gray-100 text-gray-400 transition-colors">
            <X size={18} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-6 py-6">{children}</div>
      </div>
    </div>
  );
}
