"use client";

import { useEffect } from "react";

function reportError(error: Error & { digest?: string }) {
  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || "";
  if (!backendUrl) return;

  const payload = {
    message: error.message || "Unknown error",
    stack: error.stack?.slice(0, 10000),
    page: typeof window !== "undefined" ? window.location.pathname : undefined,
    user_agent:
      typeof navigator !== "undefined" ? navigator.userAgent : undefined,
    timestamp: new Date().toISOString(),
  };

  fetch(`${backendUrl}/api/errors`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).catch(() => {});
}

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("Unhandled error:", error);
    reportError(error);
  }, [error]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="max-w-md w-full p-6 bg-white rounded-lg border border-red-200 text-center">
        <h2 className="text-lg font-semibold text-gray-900 mb-2">
          Something went wrong
        </h2>
        <p className="text-sm text-gray-500 mb-4">
          An unexpected error occurred. Please try again.
        </p>
        <button
          onClick={reset}
          className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
        >
          Try again
        </button>
      </div>
    </div>
  );
}
