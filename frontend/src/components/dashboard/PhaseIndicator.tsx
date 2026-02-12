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
    <div className="mb-6">
      <div className="flex items-center gap-2">
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
            <div key={step.key} className="flex items-center gap-2">
              <div className={`flex items-center justify-center w-8 h-8 rounded-full text-sm font-medium ${circleClass}`}>
                {isComplete ? "\u2713" : i + 1}
              </div>
              <span className={`text-sm ${labelClass}`}>
                {step.label}
              </span>
              {i < STEPS.length - 1 && (
                <div className={`w-8 h-0.5 ${lineClass}`} />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
