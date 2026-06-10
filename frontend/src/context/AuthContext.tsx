import React, { createContext, useContext, useEffect, useState } from "react";
import { getProfile, logout as apiLogout, updateProfile } from "../api/client";
import i18n from "../i18n/index";
import type { ClientProfile } from "../types";

interface AuthState {
  token: string | null;
  client: ClientProfile | null;
  loading: boolean;
  signIn: (token: string) => Promise<void>;
  signOut: () => Promise<void>;
  refreshProfile: () => Promise<void>;
  changeLanguage: (lang: string) => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(
    () => localStorage.getItem("access_token")
  );
  const [client, setClient] = useState<ClientProfile | null>(null);
  const [loading, setLoading] = useState(true);

  async function loadProfile() {
    try {
      const profile = await getProfile();
      setClient(profile);
      const lang = profile.dashboard_language || "en";
      localStorage.setItem("lang", lang);
      i18n.changeLanguage(lang);
    } catch {
      setToken(null);
      setClient(null);
      localStorage.removeItem("access_token");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (token) {
      loadProfile();
    } else {
      setLoading(false);
    }
  }, []);

  async function signIn(newToken: string) {
    localStorage.setItem("access_token", newToken);
    setToken(newToken);
    await loadProfile();
  }

  async function signOut() {
    try {
      await apiLogout();
    } catch {
      // token may already be expired — clear client-side state regardless
    } finally {
      localStorage.removeItem("access_token");
      setToken(null);
      setClient(null);
    }
  }

  async function refreshProfile() {
    await loadProfile();
  }

  async function changeLanguage(lang: string) {
    localStorage.setItem("lang", lang);
    i18n.changeLanguage(lang);
    setClient((prev) => prev ? { ...prev, dashboard_language: lang } : prev);
    await updateProfile({ dashboard_language: lang });
  }

  return (
    <AuthContext.Provider
      value={{ token, client, loading, signIn, signOut, refreshProfile, changeLanguage }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
