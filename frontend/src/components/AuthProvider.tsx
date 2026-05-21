"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import { useRouter, usePathname } from "next/navigation";
import {
  AuthContext,
  AuthState,
  isLocalMode,
  isHostedUI,
  getStoredToken,
  getStoredTenantId,
  storeAuth,
  clearAuth,
  parseTenantFromToken,
  getTokenExpiryMs,
  buildLoginUrl,
  buildLogoutUrl,
} from "@/lib/auth";

const PUBLIC_PATHS = ["/login", "/auth/callback"];

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [token, setToken] = useState<string | null>(null);
  const [tenantId, setTenantId] = useState("local-dev");
  const [ready, setReady] = useState(false);

  const localMode = isLocalMode();
  const hostedUI = isHostedUI();
  const isPublicPage = PUBLIC_PATHS.includes(pathname);

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

  // Schedule re-login 60s before token expires
  useEffect(() => {
    if (localMode || !token) return;
    const remainingMs = getTokenExpiryMs(token);
    if (remainingMs === null) return;

    const doRelogin = () => {
      clearAuth();
      if (hostedUI) {
        window.location.href = buildLoginUrl();
      } else {
        router.push("/login");
      }
    };

    if (remainingMs <= 0) {
      doRelogin();
      return;
    }
    const bufferMs = 60_000;
    const timer = setTimeout(doRelogin, Math.max(remainingMs - bufferMs, 0));
    return () => clearTimeout(timer);
  }, [localMode, hostedUI, token, router]);

  const login = useCallback(() => {
    if (!localMode) {
      if (hostedUI) {
        window.location.href = buildLoginUrl();
      } else {
        router.push("/login");
      }
    }
  }, [localMode, hostedUI, router]);

  const logout = useCallback(() => {
    clearAuth();
    setToken(null);
    setTenantId("local-dev");
    if (!localMode) {
      if (hostedUI) {
        window.location.href = buildLogoutUrl();
      } else {
        router.push("/login");
      }
    }
  }, [localMode, hostedUI, router]);

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

  // Redirect to login if not authenticated (skip for public pages)
  useEffect(() => {
    if (ready && !localMode && !token && !isPublicPage) {
      login();
    }
  }, [ready, localMode, token, isPublicPage, login]);

  if (!ready) return null;
  if (!localMode && !token && !isPublicPage) return null;

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
