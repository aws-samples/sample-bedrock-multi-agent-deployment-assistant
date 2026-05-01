"use client";

import { createContext, useContext } from "react";

export interface AuthState {
  mode: "local" | "cognito";
  tenantId: string;
  token: string | null;
  isAuthenticated: boolean;
  login: () => void;
  logout: () => void;
}

const COGNITO_DOMAIN = process.env.NEXT_PUBLIC_COGNITO_DOMAIN || "";
const COGNITO_CLIENT_ID = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID || "";
const COGNITO_REDIRECT_URI = process.env.NEXT_PUBLIC_COGNITO_REDIRECT_URI || "";

export function isLocalMode(): boolean {
  return !COGNITO_DOMAIN || !COGNITO_CLIENT_ID;
}

export function getStoredToken(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem("ai_deploy_token");
}

export function getStoredTenantId(): string {
  if (typeof window === "undefined") return "local-dev";
  return sessionStorage.getItem("ai_deploy_tenant_id") || "local-dev";
}

export function storeAuth(token: string, tenantId: string): void {
  sessionStorage.setItem("ai_deploy_token", token);
  sessionStorage.setItem("ai_deploy_tenant_id", tenantId);
}

export function clearAuth(): void {
  sessionStorage.removeItem("ai_deploy_token");
  sessionStorage.removeItem("ai_deploy_tenant_id");
}

export function parseTenantFromToken(token: string): string {
  try {
    const payload = token.split(".")[1];
    const decoded = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/")));
    return decoded["custom:tenant_id"] || decoded.sub || "default";
  } catch {
    return "default";
  }
}

export function buildLoginUrl(): string {
  const redirectUri = COGNITO_REDIRECT_URI || `${window.location.origin}/auth/callback`;
  return `${COGNITO_DOMAIN}/login?client_id=${COGNITO_CLIENT_ID}&response_type=token&scope=openid+email+profile&redirect_uri=${encodeURIComponent(redirectUri)}`;
}

export function buildLogoutUrl(): string {
  const redirectUri = COGNITO_REDIRECT_URI || `${window.location.origin}`;
  return `${COGNITO_DOMAIN}/logout?client_id=${COGNITO_CLIENT_ID}&logout_uri=${encodeURIComponent(redirectUri)}`;
}

export const AuthContext = createContext<AuthState>({
  mode: "local",
  tenantId: "local-dev",
  token: null,
  isAuthenticated: false,
  login: () => {},
  logout: () => {},
});

export function useAuth(): AuthState {
  return useContext(AuthContext);
}
