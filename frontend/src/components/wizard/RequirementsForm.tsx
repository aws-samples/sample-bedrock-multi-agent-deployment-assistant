"use client";

import { useState, useEffect } from "react";
import type { RequirementsSeed, UseCases } from "@/lib/types";
import { StepContainer } from "./StepContainer";

interface RequirementsFormProps {
  onSubmit: (data: RequirementsSeed) => void;
  loading?: boolean;
  error?: string | null;
}

interface UseCaseOption {
  value: UseCases;
  label: string;
  available: boolean;
}

// Fallback used when the config endpoint is unreachable
const DEFAULT_USE_CASES: UseCaseOption[] = [
  { value: "sd-wan", label: "SD-WAN", available: true },
  { value: "egress", label: "Egress", available: true },
  { value: "ingress", label: "Ingress", available: true },
  { value: "inspection", label: "Inspection", available: true },
];

const INPUT =
  "w-full px-3 py-2 bg-white text-gray-900 border border-gray-300 rounded-lg text-sm outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 placeholder:text-gray-400";

export function RequirementsForm({
  onSubmit,
  loading = false,
  error = null,
}: RequirementsFormProps) {
  const [useCases, setUseCases] = useState<UseCaseOption[]>(DEFAULT_USE_CASES);

  useEffect(() => {
    const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";
    fetch(`${backendUrl}/api/config/use-cases`)
      .then((res) => (res.ok ? res.json() : Promise.reject(res)))
      .then((data: UseCaseOption[]) => {
        if (Array.isArray(data) && data.length > 0) setUseCases(data);
      })
      .catch(() => {
        // Keep DEFAULT_USE_CASES on failure
      });
  }, []);

  const [form, setForm] = useState<RequirementsSeed>({
    use_cases: ["sd-wan"],
    bandwidth: 1000,
    solution_description: "",
  });

  const [validationErrors, setValidationErrors] = useState<string[]>([]);

  function toggleUseCase(uc: UseCases) {
    setForm((prev) => {
      const current = prev.use_cases;
      if (current.includes(uc)) {
        const filtered = current.filter((v) => v !== uc);
        return { ...prev, use_cases: filtered.length > 0 ? filtered : current };
      }
      return { ...prev, use_cases: [...current, uc] };
    });
  }

  function handleSubmit() {
    const errors: string[] = [];
    if (form.use_cases.length === 0) errors.push("Select at least one use case.");
    if (!form.solution_description.trim()) errors.push("Solution description is required.");
    if (form.bandwidth <= 0) errors.push("Bandwidth must be greater than 0.");

    if (errors.length > 0) {
      setValidationErrors(errors);
      return;
    }
    setValidationErrors([]);
    onSubmit(form);
  }

  return (
    <StepContainer
      title="Deployment Requirements"
      description="Tell us the basics and our AI architect will guide you through the rest."
      onNext={handleSubmit}
      nextLabel={loading ? "Starting Interview..." : "Start Interview"}
      loading={loading}
      error={error || (validationErrors.length > 0 ? validationErrors.join(" ") : null)}
    >
      <div className="space-y-5">
        {/* Use Cases (multi-select) */}
        <div>
          <label className="block text-sm font-medium text-gray-900 mb-1.5">
            Use Cases
          </label>
          <div className="flex flex-wrap gap-2">
            {useCases.map((uc) => (
              <button
                key={uc.value}
                type="button"
                disabled={!uc.available}
                onClick={() => toggleUseCase(uc.value)}
                className={`px-3 py-1.5 text-sm rounded-lg border transition-colors ${
                  form.use_cases.includes(uc.value)
                    ? "bg-blue-600 text-white border-blue-600"
                    : "bg-white text-gray-700 border-gray-300 hover:border-blue-400"
                } ${!uc.available ? "opacity-50 cursor-not-allowed" : "cursor-pointer"}`}
              >
                {uc.label}
              </button>
            ))}
          </div>
        </div>

        {/* Solution Description */}
        <div>
          <label className="block text-sm font-medium text-gray-900 mb-1.5">
            Solution Description
          </label>
          <textarea
            value={form.solution_description}
            onChange={(e) => setForm((prev) => ({ ...prev, solution_description: e.target.value }))}
            rows={3}
            placeholder="Describe your deployment objectives and requirements..."
            className={INPUT}
          />
        </div>

        {/* Bandwidth */}
        <div>
          <label className="block text-sm font-medium text-gray-900 mb-1.5">
            Bandwidth (Mbps)
          </label>
          <input
            type="number"
            min={1}
            step={100}
            value={form.bandwidth}
            onChange={(e) =>
              setForm((prev) => ({ ...prev, bandwidth: Number(e.target.value) }))
            }
            className={INPUT}
          />
        </div>
      </div>
    </StepContainer>
  );
}
