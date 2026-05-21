"use client";

import { useState, useCallback } from "react";
import type { RefinementPlan, DeploymentParameters, RefinementField } from "@/lib/types";
import { StepContainer } from "./StepContainer";

interface DeploymentParametersFormProps {
  refinementPlan: RefinementPlan;
  projectName: string;
  onSubmit: (params: DeploymentParameters) => void;
  onBack: () => void;
  loading?: boolean;
  error?: string | null;
}

const AWS_REGIONS = [
  "us-east-1",
  "us-east-2",
  "us-west-1",
  "us-west-2",
  "eu-west-1",
  "eu-west-2",
  "eu-central-1",
  "ap-southeast-1",
  "ap-southeast-2",
  "ap-northeast-1",
];

const ENVIRONMENTS = ["development", "staging", "production"];

const CIDR_PATTERN = /^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$/;

function getDefaultForField(field: RefinementField): string {
  return field.default_value ?? "";
}

export function DeploymentParametersForm({
  refinementPlan,
  projectName,
  onSubmit,
  onBack,
  loading = false,
  error = null,
}: DeploymentParametersFormProps) {
  const [awsRegion, setAwsRegion] = useState("us-east-1");
  const [vpcCidr, setVpcCidr] = useState("10.0.0.0/16");
  const [environment, setEnvironment] = useState("production");
  const [name, setName] = useState(projectName);

  const [additionalValues, setAdditionalValues] = useState<Record<string, string>>(() => {
    const defaults: Record<string, string> = {};
    for (const field of refinementPlan.fields) {
      defaults[field.field_name] = getDefaultForField(field);
    }
    return defaults;
  });

  const [validationErrors, setValidationErrors] = useState<Record<string, string>>({});

  const updateAdditional = useCallback((fieldName: string, value: string) => {
    setAdditionalValues((prev) => ({ ...prev, [fieldName]: value }));
    setValidationErrors((prev) => {
      const next = { ...prev };
      delete next[fieldName];
      return next;
    });
  }, []);

  const validate = useCallback((): boolean => {
    const errors: Record<string, string> = {};

    if (!awsRegion.trim()) {
      errors.aws_region = "AWS region is required.";
    }
    if (!CIDR_PATTERN.test(vpcCidr)) {
      errors.vpc_cidr = "Enter a valid CIDR block (e.g., 10.0.0.0/16).";
    }
    if (!environment.trim()) {
      errors.environment = "Environment is required.";
    }
    if (!name.trim()) {
      errors.project_name = "Project name is required.";
    }

    for (const field of refinementPlan.fields) {
      const value = additionalValues[field.field_name] ?? "";
      if (field.required && !value.trim()) {
        errors[field.field_name] = `${field.label} is required.`;
      }
      if (value.trim() && field.validation_pattern) {
        try {
          if (!new RegExp(field.validation_pattern).test(value)) {
            errors[field.field_name] = `Invalid format for ${field.label}.`;
          }
        } catch {
          // Invalid regex from backend — skip validation rather than crash
        }
      }
      if (
        value.trim() &&
        field.input_type === "cidr" &&
        !CIDR_PATTERN.test(value)
      ) {
        errors[field.field_name] = "Enter a valid CIDR block.";
      }
    }

    setValidationErrors(errors);
    return Object.keys(errors).length === 0;
  }, [awsRegion, vpcCidr, environment, name, refinementPlan.fields, additionalValues]);

  function handleSubmit() {
    if (!validate()) return;

    const additional: Record<string, unknown> = {};
    for (const field of refinementPlan.fields) {
      const raw = additionalValues[field.field_name] ?? "";
      if (field.input_type === "number") {
        additional[field.field_name] = raw ? Number(raw) : null;
      } else {
        additional[field.field_name] = raw || null;
      }
    }

    onSubmit({
      aws_region: awsRegion,
      vpc_cidr: vpcCidr,
      environment,
      project_name: name,
      additional_parameters: additional,
    });
  }

  function renderFieldInput(field: RefinementField) {
    const value = additionalValues[field.field_name] ?? "";
    const fieldError = validationErrors[field.field_name];
    const baseInputClasses =
      "w-full px-3 py-2 bg-white text-gray-900 border rounded-lg text-sm outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 placeholder:text-gray-400";

    switch (field.input_type) {
      case "select":
        return (
          <select
            value={value}
            onChange={(e) => updateAdditional(field.field_name, e.target.value)}
            className={`${baseInputClasses} ${fieldError ? "border-red-300" : "border-gray-300"}`}
            disabled={loading}
          >
            <option value="">Select...</option>
            {field.options?.map((opt) => (
              <option key={opt} value={opt}>
                {opt}
              </option>
            ))}
          </select>
        );
      case "number":
        return (
          <input
            type="number"
            value={value}
            onChange={(e) => updateAdditional(field.field_name, e.target.value)}
            className={`${baseInputClasses} ${fieldError ? "border-red-300" : "border-gray-300"}`}
            placeholder={field.default_value ?? ""}
            disabled={loading}
          />
        );
      case "cidr":
        return (
          <input
            type="text"
            value={value}
            onChange={(e) => updateAdditional(field.field_name, e.target.value)}
            className={`${baseInputClasses} ${fieldError ? "border-red-300" : "border-gray-300"}`}
            placeholder="e.g., 10.1.0.0/24"
            pattern={field.validation_pattern ?? undefined}
            disabled={loading}
          />
        );
      case "text":
      default:
        return (
          <input
            type="text"
            value={value}
            onChange={(e) => updateAdditional(field.field_name, e.target.value)}
            className={`${baseInputClasses} ${fieldError ? "border-red-300" : "border-gray-300"}`}
            placeholder={field.default_value ?? ""}
            disabled={loading}
          />
        );
    }
  }

  const baseInputClasses =
    "w-full px-3 py-2 bg-white text-gray-900 border rounded-lg text-sm outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 placeholder:text-gray-400";

  return (
    <StepContainer
      title="Deployment Parameters"
      description="Configure the deployment parameters for your selected architecture. Fields are pre-populated with recommended defaults from the knowledge base."
      onBack={onBack}
      loading={loading}
      error={error}
    >
      <div className="space-y-6">
        {/* Base fields */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {/* AWS Region */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              AWS Region <span className="text-red-500">*</span>
            </label>
            <select
              value={awsRegion}
              onChange={(e) => {
                setAwsRegion(e.target.value);
                setValidationErrors((prev) => {
                  const next = { ...prev };
                  delete next.aws_region;
                  return next;
                });
              }}
              className={`${baseInputClasses} ${validationErrors.aws_region ? "border-red-300" : "border-gray-300"}`}
              disabled={loading}
            >
              {AWS_REGIONS.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
            {validationErrors.aws_region && (
              <p className="mt-1 text-xs text-red-600">{validationErrors.aws_region}</p>
            )}
          </div>

          {/* VPC CIDR */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              VPC CIDR <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={vpcCidr}
              onChange={(e) => {
                setVpcCidr(e.target.value);
                setValidationErrors((prev) => {
                  const next = { ...prev };
                  delete next.vpc_cidr;
                  return next;
                });
              }}
              placeholder="10.0.0.0/16"
              className={`${baseInputClasses} ${validationErrors.vpc_cidr ? "border-red-300" : "border-gray-300"}`}
              disabled={loading}
            />
            {validationErrors.vpc_cidr && (
              <p className="mt-1 text-xs text-red-600">{validationErrors.vpc_cidr}</p>
            )}
          </div>

          {/* Environment */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Environment <span className="text-red-500">*</span>
            </label>
            <select
              value={environment}
              onChange={(e) => {
                setEnvironment(e.target.value);
                setValidationErrors((prev) => {
                  const next = { ...prev };
                  delete next.environment;
                  return next;
                });
              }}
              className={`${baseInputClasses} ${validationErrors.environment ? "border-red-300" : "border-gray-300"}`}
              disabled={loading}
            >
              {ENVIRONMENTS.map((env) => (
                <option key={env} value={env}>
                  {env.charAt(0).toUpperCase() + env.slice(1)}
                </option>
              ))}
            </select>
            {validationErrors.environment && (
              <p className="mt-1 text-xs text-red-600">{validationErrors.environment}</p>
            )}
          </div>

          {/* Project Name */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Project Name <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                setValidationErrors((prev) => {
                  const next = { ...prev };
                  delete next.project_name;
                  return next;
                });
              }}
              placeholder="my-project"
              className={`${baseInputClasses} ${validationErrors.project_name ? "border-red-300" : "border-gray-300"}`}
              disabled={loading}
            />
            {validationErrors.project_name && (
              <p className="mt-1 text-xs text-red-600">{validationErrors.project_name}</p>
            )}
          </div>
        </div>

        {/* Configuration notes from KB */}
        {refinementPlan.kb_configuration_notes && (
          <div className="px-4 py-3 bg-blue-50 border border-blue-200 rounded-lg">
            <p className="text-sm font-medium text-blue-800 mb-1">
              Configuration Notes
            </p>
            <p className="text-sm text-blue-700">
              {refinementPlan.kb_configuration_notes}
            </p>
          </div>
        )}

        {/* Additional fields from refinement plan */}
        {refinementPlan.fields.length > 0 && (
          <div>
            <h3 className="text-sm font-semibold text-gray-900 mb-3">
              Template Parameters
            </h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {refinementPlan.fields.map((field) => (
                <div key={field.field_name}>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    {field.label}
                    {field.required && <span className="text-red-500 ml-0.5">*</span>}
                  </label>
                  {renderFieldInput(field)}
                  {field.description && (
                    <p className="mt-1 text-xs text-gray-500">{field.description}</p>
                  )}
                  {field.default_rationale && (
                    <p className="mt-0.5 text-xs text-gray-400 italic">
                      {field.default_rationale}
                    </p>
                  )}
                  {validationErrors[field.field_name] && (
                    <p className="mt-1 text-xs text-red-600">
                      {validationErrors[field.field_name]}
                    </p>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* KB References */}
        {refinementPlan.kb_references.length > 0 && (
          <details className="group">
            <summary className="cursor-pointer text-sm font-medium text-gray-600 hover:text-gray-800 select-none">
              Knowledge Base References ({refinementPlan.kb_references.length})
            </summary>
            <div className="mt-2 space-y-2">
              {refinementPlan.kb_references.map((ref, i) => (
                <div
                  key={i}
                  className="px-3 py-2 bg-gray-50 rounded-lg border border-gray-100 text-sm"
                >
                  <a
                    href={ref.source_uri}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-blue-600 hover:underline font-medium break-all"
                  >
                    {ref.source_uri}
                  </a>
                  <p className="text-gray-600 mt-1">{ref.excerpt}</p>
                  <span className="text-xs text-gray-400">
                    Relevance: {(ref.relevance_score * 100).toFixed(0)}%
                  </span>
                </div>
              ))}
            </div>
          </details>
        )}

        {/* Template parameters found */}
        {refinementPlan.template_parameters_found.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            <span className="text-xs text-gray-500 mr-1">Template params:</span>
            {refinementPlan.template_parameters_found.map((param) => (
              <span
                key={param}
                className="px-2 py-0.5 text-xs font-medium text-gray-600 bg-gray-100 rounded-full"
              >
                {param}
              </span>
            ))}
          </div>
        )}

        {/* Submit button */}
        <div className="flex justify-end pt-2">
          <button
            onClick={handleSubmit}
            disabled={loading}
            className="px-5 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2 transition-colors"
          >
            {loading && (
              <svg
                className="animate-spin h-4 w-4"
                viewBox="0 0 24 24"
                fill="none"
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
            {loading ? "Submitting..." : "Deploy Configuration"}
          </button>
        </div>
      </div>
    </StepContainer>
  );
}
