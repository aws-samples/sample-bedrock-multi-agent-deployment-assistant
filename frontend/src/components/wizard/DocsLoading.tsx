"use client";

import type { DocsTaskStatus, DocumentationOutput } from "@/lib/types";

/**
 * Steps map 1:1 with the 3 documentation sections generated in parallel.
 * Progress is derived from actual WebSocket `docs_section` messages —
 * no fake timers.
 */
const DOCS_STEPS = [
  { key: "architecture_diagram" as const, label: "Generating architecture diagram" },
  { key: "user_guide" as const, label: "Writing deployment guide" },
];

function StepIcon({ active, done }: { active: boolean; done: boolean }) {
  const base = "h-5 w-5 shrink-0";
  if (done) {
    return (
      <svg className={`${base} text-green-500`} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
      </svg>
    );
  }
  if (active) {
    return <div className={`${base} rounded-full border-2 border-blue-500 border-t-transparent animate-spin`} />;
  }
  return <div className={`${base} rounded-full border-2 border-gray-200`} />;
}

function taskStatusLabel(status: DocsTaskStatus | string | null): string {
  switch (status) {
    case "queued":
      return "Queued -- waiting for available capacity";
    case "processing":
      return "Processing -- generating documentation";
    case "completed":
      return "Complete";
    case "failed":
      return "Failed";
    default:
      return "";
  }
}

function taskStatusColor(status: DocsTaskStatus | string | null): string {
  switch (status) {
    case "queued":
      return "text-amber-600 bg-amber-50 border-amber-200";
    case "processing":
      return "text-blue-600 bg-blue-50 border-blue-200";
    case "completed":
      return "text-green-600 bg-green-50 border-green-200";
    case "failed":
      return "text-red-600 bg-red-50 border-red-200";
    default:
      return "text-gray-600 bg-gray-50 border-gray-200";
  }
}

interface DocsLoadingProps {
  taskStatus: DocsTaskStatus | string | null;
  taskId?: string | null;
  /** Current partial docs output — used to derive real completion state. */
  docs?: DocumentationOutput | null;
}

export function DocsLoading({ taskStatus, taskId, docs }: DocsLoadingProps) {
  // Derive completion from actual docs content (set by WebSocket docs_section messages)
  const completedCount = DOCS_STEPS.filter(
    (s) => docs?.[s.key] && docs[s.key].length > 0,
  ).length;

  const isProcessing = taskStatus === "queued" || taskStatus === "processing";

  return (
    <div className="mt-2">
      <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
        {/* Header */}
        <div className="px-6 py-5 border-b border-gray-100">
          <div className="flex items-center gap-3">
            <div className="h-10 w-10 rounded-lg bg-blue-50 flex items-center justify-center">
              <svg className="h-5 w-5 text-blue-600" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
              </svg>
            </div>
            <div>
              <h2 className="text-lg font-semibold text-gray-900">
                Generating Documentation
              </h2>
              <p className="text-sm text-gray-500">
                Creating architecture diagram and deployment guide.
              </p>
            </div>
          </div>
        </div>

        {/* Task status badge */}
        {taskStatus && (
          <div className="px-6 pt-4">
            <div className={`inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium border rounded-full ${taskStatusColor(taskStatus)}`}>
              {isProcessing && (
                <div className="h-3.5 w-3.5 rounded-full border-2 border-current border-t-transparent animate-spin" />
              )}
              {taskStatusLabel(taskStatus)}
            </div>
            {taskId && (
              <p className="mt-1 text-xs text-gray-400 font-mono">
                Task: {taskId}
              </p>
            )}
          </div>
        )}

        {/* Progress steps — driven by real section completion */}
        <div className="px-6 py-6">
          <div className="space-y-4">
            {DOCS_STEPS.map((s) => {
              const done = !!(docs?.[s.key] && docs[s.key].length > 0);
              const active = isProcessing && !done;

              let textColor = "text-gray-400";
              if (active) {
                textColor = "text-blue-700 font-medium";
              } else if (done) {
                textColor = "text-green-700";
              }

              return (
                <div
                  key={s.key}
                  className={`flex items-center gap-3 transition-opacity duration-500 ${
                    done || active ? "opacity-100" : "opacity-30"
                  }`}
                >
                  <StepIcon active={active} done={done} />
                  <span className={`text-sm transition-colors duration-300 ${textColor}`}>
                    {s.label}
                    {active && (
                      <span className="inline-flex ml-1 gap-0.5 text-blue-400">
                        <span className="animate-bounce [animation-delay:0s]">.</span>
                        <span className="animate-bounce [animation-delay:0.15s]">.</span>
                        <span className="animate-bounce [animation-delay:0.3s]">.</span>
                      </span>
                    )}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        {/* Bottom progress bar */}
        <div className="px-6 py-4 bg-gray-50 border-t border-gray-100">
          <div className="flex items-center gap-2">
            <div className="flex-1 h-1.5 bg-gray-200 rounded-full overflow-hidden">
              <div
                className="h-full bg-blue-500 rounded-full transition-all duration-1000 ease-out"
                style={{ width: `${Math.min((completedCount / DOCS_STEPS.length) * 100, 100)}%` }}
              />
            </div>
            <span className="text-xs text-gray-400 tabular-nums shrink-0">
              {completedCount} / {DOCS_STEPS.length}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
