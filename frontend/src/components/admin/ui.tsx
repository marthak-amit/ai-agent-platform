import { useEffect, useState } from "react";
import { Loader2, X } from "lucide-react";
import { problemFrom } from "../../api/admin";

// ── formatting ───────────────────────────────────────────────────────────────

export const fmtNum = (n: number | null | undefined) => (n ?? 0).toLocaleString("en-IN");
export const fmtInr = (n: number | null | undefined, digits = 0) =>
  `₹${(n ?? 0).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
export const fmtPaise = (p: number) => fmtInr(p / 100, 2);

export function fmtDate(iso: string | null | undefined, withTime = true): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  });
}

// ── layout bits ──────────────────────────────────────────────────────────────

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 mb-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">{title}</h1>
        {subtitle && <p className="text-sm text-gray-500 mt-1">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Card({ title, children, actions, className = "" }: { title?: string; children: React.ReactNode; actions?: React.ReactNode; className?: string }) {
  return (
    <section className={`bg-white rounded-2xl border border-gray-100 shadow-sm ${className}`}>
      {(title || actions) && (
        <div className="flex items-center justify-between px-5 pt-4 pb-2">
          {title && <h2 className="text-sm font-semibold text-gray-800">{title}</h2>}
          {actions}
        </div>
      )}
      <div className="p-5 pt-3">{children}</div>
    </section>
  );
}

const TONES = {
  gray: "bg-gray-100 text-gray-700",
  green: "bg-emerald-100 text-emerald-700",
  red: "bg-red-100 text-red-700",
  amber: "bg-amber-100 text-amber-700",
  blue: "bg-blue-100 text-blue-700",
} as const;

export function Badge({ tone = "gray", children }: { tone?: keyof typeof TONES; children: React.ReactNode }) {
  return <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium ${TONES[tone]}`}>{children}</span>;
}

export function statusTone(status: string | null | undefined): keyof typeof TONES {
  switch (status) {
    case "active":
    case "paid":
    case "ok":
    case "healthy":
      return "green";
    case "pending":
    case "pending_payment":
    case "payment_submitted":
    case "degraded":
      return "amber";
    case "revoked":
    case "cancelled":
    case "expired":
    case "failed":
    case "refunded":
      return "red";
    default:
      return "gray";
  }
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-gray-500 py-10 justify-center">
      <Loader2 size={16} className="animate-spin" /> {label}
    </div>
  );
}

export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  if (!message) return null;
  return (
    <div role="alert" className="flex items-center justify-between gap-3 rounded-xl bg-red-50 border border-red-100 text-red-700 text-sm px-4 py-3">
      <span>{message}</span>
      {onRetry && (
        <button onClick={onRetry} className="font-medium underline">
          Retry
        </button>
      )}
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-gray-400 text-center py-8">{children}</p>;
}

export function Button({
  children,
  variant = "primary",
  className = "",
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger" | "ghost" }) {
  const styles = {
    primary: "bg-brand-primaryDark text-white hover:bg-emerald-800",
    secondary: "bg-white border border-gray-200 text-gray-700 hover:bg-gray-50",
    danger: "bg-red-600 text-white hover:bg-red-700",
    ghost: "text-gray-600 hover:bg-gray-100",
  }[variant];
  return (
    <button
      {...rest}
      className={`inline-flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${styles} ${className}`}
    >
      {children}
    </button>
  );
}

export const inputCls =
  "w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-primary/40 focus:border-brand-primary bg-white";

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-xs font-medium text-gray-600 mb-1">{label}</span>
      {children}
      {hint && <span className="block text-[11px] text-gray-400 mt-1">{hint}</span>}
    </label>
  );
}

// ── table + pagination ───────────────────────────────────────────────────────

export function Table({ head, children }: { head: string[]; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto -mx-5">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-gray-500 border-b border-gray-100">
            {head.map((h) => (
              <th key={h} className="px-5 py-2 font-medium whitespace-nowrap">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-50">{children}</tbody>
      </table>
    </div>
  );
}

export const Td = ({ children, className = "" }: { children?: React.ReactNode; className?: string }) => (
  <td className={`px-5 py-2.5 align-top ${className}`}>{children}</td>
);

export function Pagination({ page, pageSize, total, onPage }: { page: number; pageSize: number; total: number; onPage: (p: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="flex items-center justify-between pt-4 text-xs text-gray-500">
      <span>
        {fmtNum(total)} result{total === 1 ? "" : "s"}
      </span>
      <div className="flex items-center gap-2">
        <Button variant="secondary" disabled={page <= 1} onClick={() => onPage(page - 1)}>
          Prev
        </Button>
        <span>
          {page} / {pages}
        </span>
        <Button variant="secondary" disabled={page >= pages} onClick={() => onPage(page + 1)}>
          Next
        </Button>
      </div>
    </div>
  );
}

// ── dialogs ──────────────────────────────────────────────────────────────────

export function Modal({ title, onClose, children, wide = false }: { title: string; onClose: () => void; children: React.ReactNode; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(e) => e.stopPropagation()}
        className={`bg-white rounded-2xl shadow-xl w-full ${wide ? "max-w-3xl" : "max-w-md"} max-h-[90vh] overflow-y-auto`}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
          <h3 className="font-semibold text-gray-900">{title}</h3>
          <button onClick={onClose} aria-label="Close" className="text-gray-400 hover:text-gray-600">
            <X size={18} />
          </button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}

/**
 * Confirm dialog that collects a mandatory reason (it lands in the audit log), optionally extra fields,
 * and surfaces the server's error inline instead of closing.
 */
export function ReasonDialog({
  title,
  description,
  confirmLabel,
  danger = false,
  minReason = 5,
  onSubmit,
  onClose,
  children,
}: {
  title: string;
  description?: string;
  confirmLabel: string;
  danger?: boolean;
  minReason?: number;
  onSubmit: (reason: string) => Promise<void>;
  onClose: () => void;
  children?: React.ReactNode;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onSubmit(reason.trim());
      onClose();
    } catch (err) {
      setError(problemFrom(err).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={title} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        {description && <p className="text-sm text-gray-600">{description}</p>}
        {children}
        <Field label="Reason (recorded in the audit log)">
          <textarea className={inputCls} rows={3} value={reason} onChange={(e) => setReason(e.target.value)} required minLength={minReason} maxLength={500} autoFocus />
        </Field>
        <ErrorNote message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant={danger ? "danger" : "primary"} disabled={busy || reason.trim().length < minReason}>
            {busy && <Loader2 size={14} className="animate-spin" />} {confirmLabel}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
