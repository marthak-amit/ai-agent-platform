import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { adminLogout, adminMe, getAdminToken, setAdminToken } from "../api/admin";
import type { AdminMe, AdminPermission, AdminToken } from "../types/admin";

interface AdminAuthState {
  admin: AdminMe | null;
  loading: boolean;
  /** Store a freshly issued token + profile (login, change-password). */
  signIn: (result: AdminToken) => void;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
  can: (permission: AdminPermission) => boolean;
}

const AdminAuthContext = createContext<AdminAuthState | null>(null);

export function AdminAuthProvider({ children }: { children: React.ReactNode }) {
  const [admin, setAdmin] = useState<AdminMe | null>(null);
  const [loading, setLoading] = useState<boolean>(() => getAdminToken() !== null);

  const refresh = useCallback(async () => {
    if (!getAdminToken()) {
      setAdmin(null);
      setLoading(false);
      return;
    }
    try {
      setAdmin(await adminMe());
    } catch {
      setAdminToken(null);
      setAdmin(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const onUnauthorized = () => setAdmin(null);
    window.addEventListener("admin-unauthorized", onUnauthorized);
    return () => window.removeEventListener("admin-unauthorized", onUnauthorized);
  }, [refresh]);

  const signIn = useCallback((result: AdminToken) => {
    setAdminToken(result.access_token);
    setAdmin(result.admin);
    setLoading(false);
  }, []);

  const signOut = useCallback(async () => {
    try {
      await adminLogout(); // revokes every token of this account server-side
    } catch {
      /* already expired — clear locally regardless */
    }
    setAdminToken(null);
    setAdmin(null);
  }, []);

  const value = useMemo<AdminAuthState>(
    () => ({ admin, loading, signIn, signOut, refresh, can: (p) => !!admin && admin.permissions.includes(p) }),
    [admin, loading, signIn, signOut, refresh],
  );

  return <AdminAuthContext.Provider value={value}>{children}</AdminAuthContext.Provider>;
}

export function useAdminAuth(): AdminAuthState {
  const ctx = useContext(AdminAuthContext);
  if (!ctx) throw new Error("useAdminAuth must be used inside AdminAuthProvider");
  return ctx;
}
