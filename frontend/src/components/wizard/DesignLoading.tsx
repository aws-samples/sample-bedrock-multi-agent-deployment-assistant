"use client";

import { useState, useEffect } from "react";

const STEPS = [
  { label: "Analyzing deployment requirements" },
  { label: "Evaluating deployment architectures" },
  { label: "Comparing HA and scaling patterns" },
  { label: "Generating design options" },
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

function taskStatusLabel(status: string | null): string {
  switch (status) {
    case "queued":
      return "Queued -- waiting for available capacity";
    case "processing":
      return "Processing -- AI is generating design options";
    case "completed":
      return "Complete";
    case "failed":
      return "Failed";
    default:
      return "";
  }
}

function taskStatusColor(status: string | null): string {
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

interface DesignLoadingProps {
  useCaseSummary?: string;
  taskStatus?: string | null;
  taskId?: string | null;
}

export function DesignLoading({ useCaseSummary, taskStatus, taskId }: DesignLoadingProps) {
  // Derive minimum step from task status; animation can only advance forward
  const statusStep = taskStatus === "processing" ? 1 : 0;
  const [animatedStep, setAnimatedStep] = useState(0);
  const activeStep = Math.max(statusStep, animatedStep);

  useEffect(() => {
    const timers: ReturnType<typeof setTimeout>[] = [];
    // Stagger step transitions: 3s, 6s, 10s
    const delays = [3000, 6000, 10000];
    delays.forEach((delay, i) => {
      timers.push(setTimeout(() => setAnimatedStep((prev) => Math.max(prev, i + 1)), delay));
    });
    return () => timers.forEach(clearTimeout);
  }, []);

  return (
    <div className="mt-6">
      <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
        {/* Header */}
        <div className="px-6 py-5 border-b border-gray-100">
          <div className="flex items-center gap-3">
            <div className="h-10 w-10 rounded-lg bg-blue-50 flex items-center justify-center">
              <svg className="h-5 w-5 text-blue-600" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z" />
              </svg>
            </div>
            <div>
              <h2 className="text-lg font-semibold text-gray-900">
                Generating Design Options
              </h2>
              <p className="text-sm text-gray-500">
                The AI is creating 2-3 architecture options tailored to your requirements.
              </p>
            </div>
          </div>
        </div>

        {/* Task status badge */}
        {taskStatus && (
          <div className="px-6 pt-4">
            <div className={`inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium border rounded-full ${taskStatusColor(taskStatus)}`}>
              {taskStatus === "queued" && (
                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              )}
              {taskStatus === "processing" && (
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

        {/* Progress steps */}
        <div className="px-6 py-6">
          {useCaseSummary && (
            <div className="mb-6 px-4 py-3 bg-gray-50 rounded-lg text-sm text-gray-600">
              {useCaseSummary}
            </div>
          )}

          <div className="space-y-4">
            {STEPS.map((s, i) => {
              const done = i < activeStep;
              const active = i === activeStep;

              let textColor = "text-gray-400";
              if (active) {
                textColor = "text-blue-700 font-medium";
              } else if (done) {
                textColor = "text-green-700";
              }

              return (
                <div
                  key={s.label}
                  className={`flex items-center gap-3 transition-opacity duration-500 ${
                    i <= activeStep ? "opacity-100" : "opacity-30"
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

        {/* Bottom bar */}
        <div className="px-6 py-4 bg-gray-50 border-t border-gray-100">
          <div className="flex items-center gap-2">
            <div className="flex-1 h-1.5 bg-gray-200 rounded-full overflow-hidden">
              <div
                className="h-full bg-blue-500 rounded-full transition-all duration-1000 ease-out"
                style={{ width: `${Math.min(((activeStep + 1) / STEPS.length) * 100, 95)}%` }}
              />
            </div>
            <span className="text-xs text-gray-400 tabular-nums shrink-0">
              {activeStep + 1} / {STEPS.length}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
