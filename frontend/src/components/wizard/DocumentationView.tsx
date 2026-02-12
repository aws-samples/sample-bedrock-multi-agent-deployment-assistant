"use client";

import { useState, useEffect } from "react";
import type { DocumentationOutput, DocsTaskStatus } from "@/lib/types";
import { MarkdownRenderer } from "@/components/ui/MarkdownRenderer";
import { MermaidRenderer } from "@/components/ui/MermaidRenderer";
import { StepContainer } from "./StepContainer";
import { DocsLoading } from "./DocsLoading";

interface DocumentationViewProps {
  docs: DocumentationOutput | null;
  docsTaskId: string | null;
  docsTaskStatus: DocsTaskStatus | null;
  onBack: () => void;
  onReset: () => void;
  onRegenerateSection?: (section: keyof DocumentationOutput) => void;
  regeneratingSection?: keyof DocumentationOutput | null;
  loading?: boolean;
  error?: string | null;
}

type DocTab = "guide" | "threat" | "diagram";

const TABS: { key: DocTab; field: keyof DocumentationOutput; label: string }[] =
  [
    { key: "diagram", field: "architecture_diagram", label: "Architecture Diagram" },
    { key: "guide", field: "user_guide", label: "User Guide" },
    { key: "threat", field: "threat_model", label: "Threat Model" },
  ];

export function DocumentationView({
  docs,
  docsTaskId,
  docsTaskStatus,
  onBack,
  onReset,
  onRegenerateSection,
  regeneratingSection = null,
  loading = false,
  error = null,
}: DocumentationViewProps) {
  const [activeTab, setActiveTab] = useState<DocTab>("diagram");

  const isTaskActive =
    docsTaskStatus === "queued" || docsTaskStatus === "processing";
  const hasAnyContent = docs && (
    docs.architecture_diagram || docs.user_guide || docs.threat_model
  );

  // Auto-select the first available tab when content arrives
  useEffect(() => {
    if (!docs) return;
    if (docs.architecture_diagram && activeTab !== "diagram") return; // already has a selection
    if (docs.architecture_diagram) { setActiveTab("diagram"); return; }
    if (docs.user_guide) { setActiveTab("guide"); return; }
    if (docs.threat_model) { setActiveTab("threat"); return; }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docs?.architecture_diagram, docs?.user_guide, docs?.threat_model]);

  // Show pure loading state when task is active and no content yet
  if ((isTaskActive || (loading && !docs)) && !hasAnyContent) {
    return (
      <StepContainer
        title="Documentation"
        description="Generating project deliverables for your FortiGate deployment."
        error={error}
      >
        <DocsLoading taskStatus={docsTaskStatus} taskId={docsTaskId} docs={docs} />
      </StepContainer>
    );
  }

  // No docs output yet (task not started)
  if (!docs && !hasAnyContent) {
    return (
      <StepContainer
        title="Documentation"
        description="No documentation generated yet."
        onBack={onBack}
        error={error}
      >
        <div className="text-center py-8 text-gray-500">
          <p>Documentation will appear here once generation is complete.</p>
        </div>
      </StepContainer>
    );
  }

  const activeField = TABS.find((t) => t.key === activeTab)?.field;
  const isRegeneratingActiveTab = regeneratingSection != null && activeField === regeneratingSection;

  return (
    <StepContainer
      title="Documentation"
      description="Generated project deliverables for your FortiGate deployment."
      onBack={onBack}
      error={error}
    >
      <div>
        {/* Still generating banner */}
        {isTaskActive && (
          <div className="mb-4 flex items-center gap-2 px-3 py-2 bg-blue-50 border border-blue-200 rounded-lg text-sm text-blue-700">
            <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            <span>Generating remaining sections...</span>
          </div>
        )}

        {/* Tabs */}
        <div className="flex border-b border-gray-200 mb-4 overflow-x-auto">
          {TABS.map((tab) => {
            const content = docs?.[tab.field] ?? "";
            const hasContent = typeof content === "string" && content.length > 0;
            const isGenerating = isTaskActive && !hasContent;
            const isRegenerating = regeneratingSection === tab.field;
            return (
              <button
                key={tab.key}
                onClick={() => hasContent && setActiveTab(tab.key)}
                className={`px-4 py-2 text-sm font-medium whitespace-nowrap border-b-2 transition-colors ${
                  activeTab === tab.key
                    ? "border-blue-600 text-blue-600"
                    : hasContent
                      ? "border-transparent text-gray-500 hover:text-gray-700"
                      : "border-transparent text-gray-300 cursor-default"
                }`}
              >
                {tab.label}
                {(isGenerating || isRegenerating) && (
                  <span className="ml-2 inline-block h-2 w-2 rounded-full bg-blue-400 animate-pulse" />
                )}
              </button>
            );
          })}
        </div>

        {/* Content */}
        {activeTab === "diagram" && (
          isRegeneratingActiveTab ? (
            <div className="py-8 text-center text-gray-400 text-sm">
              Regenerating architecture diagram...
            </div>
          ) : docs?.architecture_diagram ? (
            <MermaidRenderer code={docs.architecture_diagram} />
          ) : isTaskActive ? (
            <div className="py-8 text-center text-gray-400 text-sm">
              Generating architecture diagram...
            </div>
          ) : null
        )}
        {activeTab === "guide" && (
          isRegeneratingActiveTab ? (
            <div className="py-8 text-center text-gray-400 text-sm">
              Regenerating user guide...
            </div>
          ) : docs?.user_guide ? (
            <MarkdownRenderer content={docs.user_guide} />
          ) : isTaskActive ? (
            <div className="py-8 text-center text-gray-400 text-sm">
              Generating user guide...
            </div>
          ) : null
        )}
        {activeTab === "threat" && (
          isRegeneratingActiveTab ? (
            <div className="py-8 text-center text-gray-400 text-sm">
              Regenerating threat model...
            </div>
          ) : docs?.threat_model ? (
            <MarkdownRenderer content={docs.threat_model} />
          ) : isTaskActive ? (
            <div className="py-8 text-center text-gray-400 text-sm">
              Generating threat model...
            </div>
          ) : null
        )}

        {/* Regenerate + Start Over */}
        {!isTaskActive && (
          <div className="mt-6 pt-4 border-t border-gray-100 flex items-center justify-between">
            {onRegenerateSection && activeField && (
              <button
                onClick={() => onRegenerateSection(activeField)}
                disabled={regeneratingSection != null}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-blue-700 bg-blue-50 border border-blue-200 rounded-lg hover:bg-blue-100 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {regeneratingSection === activeField ? (
                  <>
                    <svg className="animate-spin h-3.5 w-3.5" viewBox="0 0 24 24" fill="none">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                    Regenerating...
                  </>
                ) : (
                  <>
                    <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor">
                      <path fillRule="evenodd" d="M15.312 11.424a5.5 5.5 0 01-9.201 2.466l-.312-.311h2.433a.75.75 0 000-1.5H4.598a.75.75 0 00-.75.75v3.634a.75.75 0 001.5 0v-2.033l.262.263A7 7 0 0016.76 11.18a.75.75 0 10-1.448-.388zM4.688 8.576a5.5 5.5 0 019.201-2.466l.312.311H11.77a.75.75 0 000 1.5h3.634a.75.75 0 00.75-.75V3.537a.75.75 0 00-1.5 0v2.033l-.262-.263A7 7 0 003.24 8.82a.75.75 0 101.448.388z" clipRule="evenodd" />
                    </svg>
                    Regenerate
                  </>
                )}
              </button>
            )}
            <button
              onClick={onReset}
              className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50"
            >
              Start New Project
            </button>
          </div>
        )}
      </div>
    </StepContainer>
  );
}
