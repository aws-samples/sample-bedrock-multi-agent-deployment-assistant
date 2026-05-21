"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { storeAuth, parseTenantFromToken } from "@/lib/auth";

export default function AuthCallbackPage() {
  const router = useRouter();

  useEffect(() => {
    const hash = window.location.hash.slice(1);
    const params = new URLSearchParams(hash);
    const token = params.get("id_token") || params.get("access_token");

    if (token) {
      const tenantId = parseTenantFromToken(token);
      storeAuth(token, tenantId);
      router.replace("/");
    } else {
      // Also check query params (Authorization Code flow returns code in query)
      const query = new URLSearchParams(window.location.search);
      const error = query.get("error");
      if (error) {
        router.replace(`/?auth_error=${encodeURIComponent(error)}`);
      } else {
        router.replace("/");
      }
    }
  }, [router]);

  return (
    <div className="flex items-center justify-center min-h-screen">
      <p className="text-gray-500 text-sm">Completing sign-in...</p>
    </div>
  );
}
