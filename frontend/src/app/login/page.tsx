"use client";

import { useState, FormEvent } from "react";
import { storeAuth, parseTenantFromToken } from "@/lib/auth";

const API = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const res = await fetch(`${API}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({ detail: "Login failed" }));
        setError(data.detail || "Login failed");
        return;
      }

      const data = await res.json();
      const token = data.id_token || data.access_token;
      const tenantId = data.tenant_id || parseTenantFromToken(token);
      storeAuth(token, tenantId);
      window.location.href = "/";
    } catch {
      setError("Unable to connect to the server");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-gray-50 dark:bg-gray-900">
      <div className="w-full max-w-sm p-6 bg-white dark:bg-gray-800 rounded-lg shadow-md">
        <h1 className="text-xl font-semibold text-center text-gray-900 dark:text-white mb-6">
          AI Deploy Assistant
        </h1>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="email" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Email
            </label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          <div>
            <label htmlFor="password" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          {error && (
            <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2 px-4 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-400 text-white font-medium rounded-md transition-colors"
          >
            {loading ? "Signing in..." : "Sign in"}
          </button>
        </form>

        {process.env.NODE_ENV === "development" && (
          <p className="mt-4 text-xs text-center text-gray-500 dark:text-gray-400">
            Local dev: dev@local.test / LocalDev1!
          </p>
        )}
      </div>
    </div>
  );
}
