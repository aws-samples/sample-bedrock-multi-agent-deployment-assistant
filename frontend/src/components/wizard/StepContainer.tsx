"use client";

import { ReactNode } from "react";
import { MarkdownRenderer } from "@/components/ui/MarkdownRenderer";

interface StepContainerProps {
  title: string;
  description?: string;
  children: ReactNode;
  onNext?: () => void;
  onBack?: () => void;
  nextLabel?: string;
  loading?: boolean;
  error?: string | null;
}

export function StepContainer({
  title,
  description,
  children,
  onNext,
  onBack,
  nextLabel = "Continue",
  loading = false,
  error = null,
}: StepContainerProps) {
  return (
    <div className="bg-white rounded-lg border border-gray-200 shadow-sm">
      <div className="px-6 py-4 border-b border-gray-100">
        <h2 className="text-lg font-semibold text-gray-900">{title}</h2>
        {description && (
          <div className="text-sm text-gray-500 mt-1">
            <MarkdownRenderer content={description} />
          </div>
        )}
      </div>

      <div className="px-6 py-5">{children}</div>

      {error && (
        <div className="mx-6 mb-4 px-4 py-3 bg-red-50 border border-red-200 rounded-lg" role="alert" aria-live="assertive">
          <p className="text-sm text-red-700">{error}</p>
        </div>
      )}

      {(onBack || onNext) && (
        <div className="px-6 py-4 border-t border-gray-100 flex justify-between">
          {onBack ? (
            <button
              onClick={onBack}
              disabled={loading}
              className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50"
            >
              Back
            </button>
          ) : (
            <div />
          )}
          {onNext && (
            <button
              onClick={onNext}
              disabled={loading}
              className="px-5 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2"
            >
              {loading && (
                <svg
                  className="animate-spin h-4 w-4"
                  viewBox="0 0 24 24"
                  fill="none"
                  aria-hidden="true"
                >
                  <circle
                    className="opacity-25"
                    cx="12"
                    cy="12"
                    r="10"
                    stroke="currentColor"
                    strokeWidth="4"
                  />
                  <path
                    className="opacity-75"
                    fill="currentColor"
                    d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                  />
                </svg>
              )}
              {nextLabel}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
