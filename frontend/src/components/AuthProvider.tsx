"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import {
  AuthContext,
  AuthState,
  isLocalMode,
  getStoredToken,
  getStoredTenantId,
  storeAuth,
  clearAuth,
  parseTenantFromToken,
  buildLoginUrl,
  buildLogoutUrl,
} from "@/lib/auth";

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [tenantId, setTenantId] = useState("local-dev");
  const [ready, setReady] = useState(false);

  const localMode = isLocalMode();

  useEffect(() => {
    if (localMode) {
      setTenantId("local-dev");
      setReady(true);
      return;
    }

    // Check for token in URL hash (Cognito implicit grant callback)
    if (typeof window !== "undefined" && window.location.hash) {
      const params = new URLSearchParams(window.location.hash.slice(1));
      const accessToken = params.get("id_token") || params.get("access_token");
      if (accessToken) {
        const tenant = parseTenantFromToken(accessToken);
        storeAuth(accessToken, tenant);
        setToken(accessToken);
        setTenantId(tenant);
        window.history.replaceState(null, "", window.location.pathname);
        setReady(true);
        return;
      }
    }

    // Restore from session storage
    const storedToken = getStoredToken();
    if (storedToken) {
      setToken(storedToken);
      setTenantId(getStoredTenantId());
    }
    setReady(true);
  }, [localMode]);

  const login = useCallback(() => {
    if (!localMode) {
      window.location.href = buildLoginUrl();
    }
  }, [localMode]);

  const logout = useCallback(() => {
    clearAuth();
    setToken(null);
    setTenantId("local-dev");
    if (!localMode) {
      window.location.href = buildLogoutUrl();
    }
  }, [localMode]);

  const value: AuthState = useMemo(
    () => ({
      mode: localMode ? "local" : "cognito",
      tenantId,
      token,
      isAuthenticated: localMode || !!token,
      login,
      logout,
    }),
    [localMode, tenantId, token, login, logout],
  );

  if (!ready) return null;

  // In Cognito mode, redirect to login if no token
  if (!localMode && !token) {
    login();
    return null;
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
