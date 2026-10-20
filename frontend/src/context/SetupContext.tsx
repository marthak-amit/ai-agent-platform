import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { getChecklist } from "../api/client";
import type { Checklist } from "../types/setup";
import { useAuth } from "./AuthContext";

interface SetupContextValue {
  /** The setup checklist (status derived from real state on the server); null until loaded or for non-owners. */
  checklist: Checklist | null;
  loading: boolean;
  /** Re-fetch GET /onboarding/checklist. Resolves to the new value, or null on failure / non-owner. */
  refresh: () => Promise<Checklist | null>;
}

const SetupContext = createContext<SetupContextValue | null>(null);

/**
 * Shared setup-checklist state for the dashboard card, the empty states and the sidebar status.
 * Only the account Owner can read it; for everyone else it stays null and nothing is fetched.
 */
export function SetupProvider({ children }: { children: React.ReactNode }) {
  const { client } = useAuth();
  const clientId = client?.id ?? null;
  const isOwner = !!client?.current_user.is_owner;
  const ready = !!client && client.quick_start_completed;
  const [checklist, setChecklist] = useState<Checklist | null>(null);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);

  const refresh = useCallback(async (): Promise<Checklist | null> => {
    if (!isOwner || !ready) return null;
    const mine = ++seq.current;
    setLoading(true);
    try {
      const next = await getChecklist();
      if (mine === seq.current) setChecklist(next);
      return next;
    } catch {
      return null;
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, [isOwner, ready]);

  useEffect(() => {
    if (clientId === null) {
      seq.current++;
      setChecklist(null);
      return;
    }
    void refresh();
  }, [clientId, refresh]);

  const value = useMemo(() => ({ checklist, loading, refresh }), [checklist, loading, refresh]);
  return <SetupContext.Provider value={value}>{children}</SetupContext.Provider>;
}

export function useSetup(): SetupContextValue {
  const ctx = useContext(SetupContext);
  if (!ctx) throw new Error("useSetup must be used within SetupProvider");
  return ctx;
}
