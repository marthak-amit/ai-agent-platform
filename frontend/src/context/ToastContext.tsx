import { createContext, useCallback, useContext, useRef, useState } from "react";
import { X } from "lucide-react";

export interface ToastAction {
  label: string;
  onClick: () => void;
}

interface ToastOptions {
  kind?: "info" | "success" | "error";
  /** ms; 0 keeps it until dismissed */
  duration?: number;
  action?: ToastAction;
}

interface ToastItem extends ToastOptions {
  id: number;
  message: string;
}

interface ToastApi {
  show: (message: string, options?: ToastOptions) => number;
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const KIND_STYLES: Record<NonNullable<ToastOptions["kind"]>, string> = {
  info: "bg-brand-secondary text-white",
  success: "bg-brand-primaryDark text-white",
  error: "bg-red-600 text-white",
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const show = useCallback(
    (message: string, options: ToastOptions = {}) => {
      const id = nextId.current++;
      setToasts((prev) => [...prev, { id, message, ...options }]);
      const duration = options.duration ?? 4000;
      if (duration > 0) window.setTimeout(() => dismiss(id), duration);
      return id;
    },
    [dismiss],
  );

  return (
    <ToastContext.Provider value={{ show, dismiss }}>
      {children}
      <div
        className="fixed bottom-4 left-1/2 -translate-x-1/2 z-[100] flex flex-col gap-2 items-center w-[calc(100%-2rem)] max-w-md pointer-events-none"
        role="status"
        aria-live="polite"
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`pointer-events-auto w-full flex items-center gap-3 px-4 py-3 rounded-xl shadow-lg text-sm ${KIND_STYLES[t.kind ?? "info"]}`}
          >
            <span className="flex-1">{t.message}</span>
            {t.action && (
              <button
                onClick={() => {
                  t.action?.onClick();
                  dismiss(t.id);
                }}
                className="font-semibold underline underline-offset-2 hover:no-underline"
              >
                {t.action.label}
              </button>
            )}
            <button onClick={() => dismiss(t.id)} aria-label="Dismiss" className="opacity-70 hover:opacity-100">
              <X size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}
