"use client";

import { useState } from "react";
import type { DesignRecommendation, DesignOption, KBReference } from "@/lib/types";
import { StepContainer } from "./StepContainer";

interface DesignReviewProps {
  recommendation: DesignRecommendation;
  onApprove: (index: number) => void;
  onRequestRedesign?: (feedback: string) => void;
  onBack: () => void;
  loading?: boolean;
  error?: string | null;
}

function KBReferencesSection({ references }: { references: KBReference[] }) {
  if (references.length === 0) return null;

  return (
    <details className="mt-3 group">
      <summary className="cursor-pointer text-xs font-medium text-gray-500 hover:text-gray-700 select-none">
        Knowledge Base References ({references.length})
      </summary>
      <div className="mt-2 space-y-2">
        {references.map((ref, i) => (
          <div
            key={i}
            className="px-3 py-2 bg-gray-50 rounded-lg border border-gray-100 text-xs"
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
            <span className="text-gray-400">
              Relevance: {(ref.relevance_score * 100).toFixed(0)}%
            </span>
          </div>
        ))}
      </div>
    </details>
  );
}

function WellArchitectedBadges({
  assessment,
}: {
  assessment: Record<string, string> | null;
}) {
  if (!assessment) return null;

  const entries = Object.entries(assessment);
  if (entries.length === 0) return null;

  return (
    <div className="mt-3">
      <span className="text-xs font-medium text-gray-500 block mb-1.5">
        Well-Architected Assessment
      </span>
      <div className="flex flex-wrap gap-1.5">
        {entries.map(([pillar, rating]) => (
          <span
            key={pillar}
            className="px-2 py-0.5 text-xs font-medium text-purple-700 bg-purple-50 border border-purple-200 rounded-full"
            title={`${pillar}: ${rating}`}
          >
            {pillar}: {rating}
          </span>
        ))}
      </div>
    </div>
  );
}

function TopologySummary({ option }: { option: DesignOption }) {
  const vpcCount = option.vpc_topology?.length ?? 0;
  const fgtCount = option.fortigate_topology?.length ?? 0;

  if (vpcCount === 0 && fgtCount === 0) return null;

  return (
    <div className="mt-3 text-sm text-gray-600">
      <span className="font-medium text-gray-700">Topology:</span>{" "}
      {vpcCount} VPC{vpcCount !== 1 ? "s" : ""},{" "}
      {fgtCount} FortiGate{fgtCount !== 1 ? "s" : ""}
    </div>
  );
}

function MetadataPills({ option }: { option: DesignOption }) {
  const pills: { label: string; value: string; color: string }[] = [];

  if (option.deployment_pattern) {
    pills.push({
      label: "Pattern",
      value: option.deployment_pattern,
      color: "text-indigo-700 bg-indigo-50 border-indigo-200",
    });
  }
  if (option.ha_mode) {
    pills.push({
      label: "HA",
      value: option.ha_mode,
      color: "text-emerald-700 bg-emerald-50 border-emerald-200",
    });
  }
  if (option.has_code_template) {
    pills.push({
      label: "Template",
      value: "Available",
      color: "text-amber-700 bg-amber-50 border-amber-200",
    });
  }

  if (pills.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1.5 mt-3">
      {pills.map((pill) => (
        <span
          key={pill.label}
          className={`px-2 py-0.5 text-xs font-medium border rounded-full ${pill.color}`}
        >
          {pill.label}: {pill.value}
        </span>
      ))}
    </div>
  );
}

export function DesignReview({
  recommendation,
  onApprove,
  onRequestRedesign,
  onBack,
  loading = false,
  error = null,
}: DesignReviewProps) {
  const [showFeedback, setShowFeedback] = useState(false);
  const [feedback, setFeedback] = useState("");

  function handleRequestRedesign() {
    if (feedback.trim()) {
      onRequestRedesign?.(feedback.trim());
      setFeedback("");
      setShowFeedback(false);
    }
  }

  function handleCancelFeedback() {
    setShowFeedback(false);
    setFeedback("");
  }

  return (
    <StepContainer
      title="Architecture Design Options"
      description={recommendation.rationale}
      onBack={onBack}
      loading={loading}
      error={error}
    >
      {/* Requirements summary */}
      {recommendation.requirements_summary && (
        <div className="mb-4 px-4 py-3 bg-gray-50 rounded-lg text-sm text-gray-600">
          {recommendation.requirements_summary}
        </div>
      )}

      {/* Available templates info */}
      {recommendation.available_templates && recommendation.available_templates.length > 0 && (
        <div className="mb-4 flex flex-wrap gap-1.5 items-center">
          <span className="text-xs text-gray-500">Available templates:</span>
          {recommendation.available_templates.map((tpl) => (
            <span
              key={tpl}
              className="px-2 py-0.5 text-xs font-medium text-teal-700 bg-teal-50 border border-teal-200 rounded-full"
            >
              {tpl}
            </span>
          ))}
        </div>
      )}

      <div className="space-y-4">
        {recommendation.options.map((option, i) => {
          const isRecommended = i === recommendation.recommended_option_index;

          return (
            <div
              key={i}
              className={`p-5 border rounded-lg transition-colors ${
                isRecommended
                  ? "border-blue-500 bg-blue-50/50"
                  : "border-gray-200 hover:border-gray-300"
              }`}
            >
              <div className="flex items-start justify-between">
                <div>
                  <h3 className="font-semibold text-gray-900">
                    {option.name}
                    {isRecommended && (
                      <span className="ml-2 px-2 py-0.5 text-xs font-medium text-blue-700 bg-blue-100 rounded-full">
                        Recommended
                      </span>
                    )}
                  </h3>
                  <p className="text-sm text-gray-600 mt-1">
                    {option.description}
                  </p>
                </div>
              </div>

              <p className="text-sm text-gray-700 mt-3">
                {option.architecture_summary}
              </p>

              {/* Metadata pills */}
              <MetadataPills option={option} />

              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4 text-sm">
                <div className="bg-gray-50 p-2.5 rounded-lg">
                  <span className="text-gray-500 block text-xs">
                    Monthly Cost
                  </span>
                  <span className="font-medium text-gray-900">
                    ${option.estimated_monthly_cost_usd.toLocaleString()}
                  </span>
                </div>
                <div className="bg-gray-50 p-2.5 rounded-lg">
                  <span className="text-gray-500 block text-xs">Security</span>
                  <span className="font-medium text-gray-900">
                    {"\u2605".repeat(option.security_posture_rating)}
                    {"\u2606".repeat(5 - option.security_posture_rating)}
                  </span>
                </div>
                <div className="bg-gray-50 p-2.5 rounded-lg">
                  <span className="text-gray-500 block text-xs">
                    Complexity
                  </span>
                  <span className="font-medium text-gray-900">
                    {option.complexity_rating}/5
                  </span>
                </div>
                <div className="bg-gray-50 p-2.5 rounded-lg">
                  <span className="text-gray-500 block text-xs">Instance Type</span>
                  <span className="font-medium text-gray-900">
                    {option.fortigate_instance_type}
                  </span>
                </div>
              </div>

              {/* Topology summary */}
              <TopologySummary option={option} />

              <div className="grid grid-cols-2 gap-4 mt-3 text-sm">
                <div>
                  <span className="font-medium text-green-700">Pros</span>
                  <ul className="mt-1 space-y-1">
                    {option.pros.map((pro, j) => (
                      <li key={j} className="text-gray-600 flex items-start gap-1">
                        <span className="text-green-500 mt-0.5">+</span>
                        {pro}
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <span className="font-medium text-red-700">Cons</span>
                  <ul className="mt-1 space-y-1">
                    {option.cons.map((con, j) => (
                      <li key={j} className="text-gray-600 flex items-start gap-1">
                        <span className="text-red-500 mt-0.5">-</span>
                        {con}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>

              {/* AWS Services */}
              {option.aws_services && option.aws_services.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-1.5">
                  <span className="text-xs text-gray-500 mr-1">AWS Services:</span>
                  {option.aws_services.map((svc) => (
                    <span
                      key={svc}
                      className="px-2 py-0.5 text-xs font-medium text-gray-600 bg-gray-100 rounded-full"
                    >
                      {svc}
                    </span>
                  ))}
                </div>
              )}

              {/* Well-Architected Assessment */}
              <WellArchitectedBadges assessment={option.well_architected_assessment} />

              {/* KB References */}
              <KBReferencesSection references={option.kb_references ?? []} />

              <div className="mt-4">
                <button
                  onClick={() => onApprove(i)}
                  disabled={loading}
                  className={`px-4 py-2 text-sm font-medium rounded-lg transition-colors disabled:opacity-50 ${
                    isRecommended
                      ? "bg-blue-600 text-white hover:bg-blue-700"
                      : "bg-white text-gray-700 border border-gray-300 hover:bg-gray-50"
                  }`}
                >
                  {loading
                    ? "Selecting..."
                    : `Select ${option.name}`}
                </button>
              </div>
            </div>
          );
        })}
      </div>

      {/* Request Changes / Redesign */}
      {onRequestRedesign && (
        <div className="mt-6 pt-5 border-t border-gray-200">
          {!showFeedback ? (
            <button
              onClick={() => setShowFeedback(true)}
              disabled={loading}
              className="text-sm text-gray-500 hover:text-gray-700 underline disabled:opacity-50"
            >
              None of these work? Request new designs
            </button>
          ) : (
            <div className="space-y-3">
              <label className="block text-sm font-medium text-gray-700">
                What changes would you like to see?
              </label>
              <textarea
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                rows={3}
                placeholder="e.g., I need a lower-cost option, or I need GWLB instead of TGW routing..."
                className="w-full px-3 py-2 bg-white text-gray-900 border border-gray-300 rounded-lg text-sm outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 placeholder:text-gray-400"
              />
              <div className="flex gap-3">
                <button
                  onClick={handleRequestRedesign}
                  disabled={loading || !feedback.trim()}
                  className="px-4 py-2 text-sm font-medium bg-amber-600 text-white rounded-lg hover:bg-amber-700 disabled:opacity-50 transition-colors"
                >
                  {loading ? "Generating New Designs..." : "Generate New Designs"}
                </button>
                <button
                  onClick={handleCancelFeedback}
                  disabled={loading}
                  className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 disabled:opacity-50"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </StepContainer>
  );
}
