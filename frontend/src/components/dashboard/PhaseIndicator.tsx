"use client";

import { WizardStep } from "@/lib/types";

const STEPS: { key: WizardStep; label: string }[] = [
  { key: "requirements", label: "Requirements" },
  { key: "design", label: "Design" },
  { key: "iac", label: "IaC Generation" },
  { key: "documentation", label: "Documentation" },
];

interface PhaseIndicatorProps {
  currentStep: WizardStep;
}

export function PhaseIndicator({ currentStep }: PhaseIndicatorProps) {
  const currentIndex = STEPS.findIndex((s) => s.key === currentStep);

  return (
    <nav aria-label="Wizard progress" className="mb-6">
      <ol className="flex items-center gap-2" role="list">
        {STEPS.map((step, i) => {
          const isActive = i === currentIndex;
          const isComplete = i < currentIndex;

          let circleClass = "bg-gray-200 text-gray-500";
          if (isComplete) {
            circleClass = "bg-green-600 text-white";
          } else if (isActive) {
            circleClass = "bg-blue-600 text-white";
          }

          const labelClass = isActive
            ? "font-semibold text-gray-900"
            : "text-gray-500";

          const lineClass = isComplete ? "bg-green-600" : "bg-gray-200";

          return (
            <li
              key={step.key}
              className="flex items-center gap-2"
              aria-current={isActive ? "step" : undefined}
            >
              <div
                className={`flex items-center justify-center w-8 h-8 rounded-full text-sm font-medium ${circleClass}`}
                aria-hidden="true"
              >
                {isComplete ? "\u2713" : i + 1}
              </div>
              <span className={`text-sm ${labelClass}`}>
                {step.label}
                {isComplete && <span className="sr-only"> (completed)</span>}
                {isActive && <span className="sr-only"> (current)</span>}
              </span>
              {i < STEPS.length - 1 && (
                <div className={`w-8 h-0.5 ${lineClass}`} aria-hidden="true" />
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
