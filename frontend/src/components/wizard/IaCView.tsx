"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  IaCOutput,
  IaCTaskStatus,
  ValidationFinding,
  ValidationReport,
} from "@/lib/types";
import { getIaCTask } from "@/lib/api";
import { CodeBlock } from "@/components/ui/CodeBlock";
import { StepContainer } from "./StepContainer";

const API = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

const IAC_POLL_INTERVAL_MS = 3_000;
const IAC_POLL_MAX_ATTEMPTS = 120; // 6 minutes max

interface IaCViewProps {
  iac: IaCOutput | null;
  iacTaskId?: string | null;
  iacTaskStatus?: IaCTaskStatus | null;
  onIaCComplete?: (result: unknown) => void;
  onIaCFailed?: (error: string) => void;
  onRegenerateIaC?: (feedback: string) => void;
  onContinue: () => void;
  onBack: () => void;
  loading?: boolean;
  error?: string | null;
  projectId?: string;
  tenantId?: string;
  wsConnected?: boolean;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface FileGroup {
  label: string;
  files: { path: string; name: string; lang: string }[];
}

function langForFile(path: string): string {
  if (path.endsWith(".yaml") || path.endsWith(".yml")) return "yaml";
  if (path.endsWith(".json")) return "json";
  if (path.endsWith(".guard")) return "hcl";
  return "yaml";
}

function getGroupKey(parts: string[]): { key: string; label: string } {
  if (parts[0] === "cloudformation") {
    return { key: "cloudformation", label: "CloudFormation Templates" };
  }
  if (parts[0] === "guard" || parts[parts.length - 1]?.endsWith(".guard")) {
    return { key: "guard", label: "Guard Rules" };
  }
  return { key: "root", label: "Generated Files" };
}

function sortGroupKey(key: string): string {
  if (key === "cloudformation") return "0";
  if (key === "root") return "1";
  return "2";
}

function groupFiles(paths: string[]): FileGroup[] {
  const groups: Record<string, FileGroup> = {};

  for (const path of paths) {
    const parts = path.split("/");
    const { key, label } = getGroupKey(parts);

    if (!groups[key]) {
      groups[key] = { label, files: [] };
    }
    groups[key].files.push({
      path,
      name: parts[parts.length - 1],
      lang: langForFile(path),
    });
  }

  return Object.entries(groups)
    .sort(([a], [b]) => sortGroupKey(a).localeCompare(sortGroupKey(b)))
    .map(([, g]) => g);
}

function resolutionPathLabel(path: string): string {
  switch (path) {
    case "parameterize":
      return "Template Parameterization";
    case "compose":
      return "Snippet Composition";
    case "generate":
      return "AI Generation";
    default:
      return path;
  }
}

function severityColor(severity: string): string {
  switch (severity) {
    case "error":
      return "text-red-700 bg-red-50";
    case "warning":
      return "text-amber-700 bg-amber-50";
    case "info":
      return "text-blue-700 bg-blue-50";
    default:
      return "text-gray-700 bg-gray-50";
  }
}

function severityIcon(severity: string): string {
  switch (severity) {
    case "error":
      return "X";
    case "warning":
      return "!";
    case "info":
      return "i";
    default:
      return "?";
  }
}

// ---------------------------------------------------------------------------
// IaC Loading component (matches DesignLoading pattern)
// ---------------------------------------------------------------------------

const IAC_STEPS = [
  { label: "Resolving templates and parameters" },
  { label: "Generating CloudFormation templates" },
  { label: "Running validation pipeline" },
  { label: "Applying fixes and finalizing" },
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

function taskStatusLabel(status: IaCTaskStatus | string | null): string {
  switch (status) {
    case "queued":
      return "Queued -- waiting for available capacity";
    case "processing":
      return "Processing -- generating infrastructure code";
    case "validating":
      return "Validating -- running security and lint checks";
    case "completed":
      return "Complete";
    case "failed":
      return "Failed";
    default:
      return "";
  }
}

function taskStatusColor(status: IaCTaskStatus | string | null): string {
  switch (status) {
    case "queued":
      return "text-amber-600 bg-amber-50 border-amber-200";
    case "processing":
      return "text-blue-600 bg-blue-50 border-blue-200";
    case "validating":
      return "text-indigo-600 bg-indigo-50 border-indigo-200";
    case "completed":
      return "text-green-600 bg-green-50 border-green-200";
    case "failed":
      return "text-red-600 bg-red-50 border-red-200";
    default:
      return "text-gray-600 bg-gray-50 border-gray-200";
  }
}

function statusToStep(status: IaCTaskStatus | string | null): number {
  switch (status) {
    case "queued":
      return 0;
    case "processing":
      return 1;
    case "validating":
      return 2;
    case "completed":
      return 3;
    default:
      return 0;
  }
}

function IaCLoading({
  taskStatus,
  taskId,
}: {
  taskStatus: IaCTaskStatus | string | null;
  taskId?: string | null;
}) {
  // Derive minimum step from task status; animation can only advance forward
  const statusStep = statusToStep(taskStatus);
  const [animatedStep, setAnimatedStep] = useState(0);
  const activeStep = Math.max(statusStep, animatedStep);

  // Stagger step transitions as fallback
  useEffect(() => {
    const timers: ReturnType<typeof setTimeout>[] = [];
    const delays = [4000, 10000, 20000];
    delays.forEach((delay, i) => {
      timers.push(
        setTimeout(() => setAnimatedStep((prev) => Math.max(prev, i + 1)), delay),
      );
    });
    return () => timers.forEach(clearTimeout);
  }, []);

  return (
    <div className="mt-2">
      <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
        {/* Header */}
        <div className="px-6 py-5 border-b border-gray-100">
          <div className="flex items-center gap-3">
            <div className="h-10 w-10 rounded-lg bg-blue-50 flex items-center justify-center">
              <svg className="h-5 w-5 text-blue-600" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M17.25 6.75L22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3l-4.5 16.5" />
              </svg>
            </div>
            <div>
              <h2 className="text-lg font-semibold text-gray-900">
                Generating Infrastructure Code
              </h2>
              <p className="text-sm text-gray-500">
                Creating validated CloudFormation templates from your design.
              </p>
            </div>
          </div>
        </div>

        {/* Task status badge */}
        {taskStatus && (
          <div className="px-6 pt-4">
            <div className={`inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium border rounded-full ${taskStatusColor(taskStatus)}`}>
              {(taskStatus === "queued" || taskStatus === "processing" || taskStatus === "validating") && (
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
          <div className="space-y-4">
            {IAC_STEPS.map((s, i) => {
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

        {/* Bottom progress bar */}
        <div className="px-6 py-4 bg-gray-50 border-t border-gray-100">
          <div className="flex items-center gap-2">
            <div className="flex-1 h-1.5 bg-gray-200 rounded-full overflow-hidden">
              <div
                className="h-full bg-blue-500 rounded-full transition-all duration-1000 ease-out"
                style={{ width: `${Math.min(((activeStep + 1) / IAC_STEPS.length) * 100, 95)}%` }}
              />
            </div>
            <span className="text-xs text-gray-400 tabular-nums shrink-0">
              {activeStep + 1} / {IAC_STEPS.length}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Validation Report component
// ---------------------------------------------------------------------------

function ValidationReportView({ report }: { report: ValidationReport }) {
  const errorCount = report.findings.filter((f) => f.severity === "error").length;
  const warningCount = report.findings.filter((f) => f.severity === "warning").length;
  const infoCount = report.findings.filter((f) => f.severity === "info").length;

  // Group findings by layer
  const findingsByLayer = useMemo(() => {
    const groups: Record<string, ValidationFinding[]> = {};
    for (const f of report.findings) {
      if (!groups[f.layer]) groups[f.layer] = [];
      groups[f.layer].push(f);
    }
    return groups;
  }, [report.findings]);

  return (
    <div className="space-y-3">
      {/* Summary bar */}
      <div className="flex items-center gap-4 text-sm">
        <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${
          report.passed
            ? "bg-green-50 text-green-700 border border-green-200"
            : "bg-red-50 text-red-700 border border-red-200"
        }`}>
          {report.passed ? "Passed" : "Failed"}
        </span>
        {errorCount > 0 && (
          <span className="text-red-600 text-xs">{errorCount} error{errorCount !== 1 ? "s" : ""}</span>
        )}
        {warningCount > 0 && (
          <span className="text-amber-600 text-xs">{warningCount} warning{warningCount !== 1 ? "s" : ""}</span>
        )}
        {infoCount > 0 && (
          <span className="text-blue-600 text-xs">{infoCount} info</span>
        )}
        {report.fix_attempts > 0 && (
          <span className="text-gray-500 text-xs">
            {report.fix_attempts} fix attempt{report.fix_attempts !== 1 ? "s" : ""}
          </span>
        )}
      </div>

      {/* Layers executed */}
      {report.layers_executed.length > 0 && (
        <div className="flex items-center gap-2 text-xs text-gray-500">
          <span className="font-medium">Layers:</span>
          {report.layers_executed.map((layer) => (
            <span key={layer} className="px-2 py-0.5 bg-gray-100 rounded text-gray-600">
              {layer}
            </span>
          ))}
        </div>
      )}

      {/* Findings by layer */}
      {Object.entries(findingsByLayer).map(([layer, findings]) => (
        <div key={layer} className="border border-gray-200 rounded-lg overflow-hidden">
          <div className="px-3 py-2 bg-gray-50 border-b border-gray-200">
            <span className="text-xs font-semibold text-gray-600 uppercase tracking-wide">
              {layer}
            </span>
            <span className="ml-2 text-xs text-gray-400">
              {findings.length} finding{findings.length !== 1 ? "s" : ""}
            </span>
          </div>
          <div className="divide-y divide-gray-100">
            {findings.map((f, idx) => (
              <div key={`${f.rule_id}-${idx}`} className="px-3 py-2 text-xs">
                <div className="flex items-start gap-2">
                  <span className={`inline-flex items-center justify-center w-4 h-4 rounded-full text-[10px] font-bold shrink-0 mt-0.5 ${severityColor(f.severity)}`}>
                    {severityIcon(f.severity)}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-gray-500">{f.rule_id}</span>
                      {f.resource && (
                        <span className="text-gray-400 truncate">{f.resource}</span>
                      )}
                      {f.line != null && (
                        <span className="text-gray-400">L{f.line}</span>
                      )}
                    </div>
                    <p className="text-gray-700 mt-0.5">{f.message}</p>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}

      {report.findings.length === 0 && (
        <p className="text-sm text-green-600">No issues found. All validation layers passed.</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function IaCView({
  iac,
  iacTaskId,
  iacTaskStatus,
  onIaCComplete,
  onIaCFailed,
  onRegenerateIaC,
  onContinue,
  onBack,
  loading = false,
  error = null,
  projectId,
  tenantId = "default",
  wsConnected = false,
}: IaCViewProps) {
  const fileCount = iac ? Object.keys(iac.files).length : 0;
  const groups = useMemo(
    () => groupFiles(iac ? Object.keys(iac.files) : []),
    [iac],
  );
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const [expandedFiles, setExpandedFiles] = useState<Set<string>>(new Set());
  const [showValidation, setShowValidation] = useState(false);
  const [showFeedback, setShowFeedback] = useState(false);
  const [feedback, setFeedback] = useState("");
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wsConnectedRef = useRef(wsConnected);
  useEffect(() => { wsConnectedRef.current = wsConnected; });

  const handleRegenerateIaC = useCallback(() => {
    if (feedback.trim()) {
      onRegenerateIaC?.(feedback.trim());
    }
  }, [feedback, onRegenerateIaC]);

  const handleCancelFeedback = useCallback(() => {
    setShowFeedback(false);
    setFeedback("");
  }, []);

  const selectedFile =
    activeFile && iac?.files[activeFile]
      ? activeFile
      : groups[0]?.files[0]?.path ?? null;

  const isTaskActive =
    iacTaskStatus === "queued" ||
    iacTaskStatus === "processing" ||
    iacTaskStatus === "validating";
  const isComplete = !!iac && fileCount > 0 && !isTaskActive;

  // ---------------------------------------------------------------------------
  // Poll for IaC task status when WebSocket is unavailable
  // ---------------------------------------------------------------------------
  const onIaCCompleteRef = useRef(onIaCComplete);
  const onIaCFailedRef = useRef(onIaCFailed);
  useEffect(() => {
    onIaCCompleteRef.current = onIaCComplete;
    onIaCFailedRef.current = onIaCFailed;
  });

  const pollIaCTaskRef = useRef<(taskId: string, attempt?: number) => Promise<void>>();
  const pollIaCTask = useCallback(
    async (taskId: string, attempt = 0) => {
      // WebSocket connected — let it handle updates instead
      if (wsConnectedRef.current) return;
      if (attempt >= IAC_POLL_MAX_ATTEMPTS) {
        onIaCFailedRef.current?.("IaC task timed out. Please try again.");
        return;
      }

      try {
        const task = await getIaCTask(taskId, tenantId);

        if (task.status === "completed" && task.result) {
          onIaCCompleteRef.current?.(task.result);
          return;
        }

        if (task.status === "failed") {
          onIaCFailedRef.current?.(task.error ?? "IaC task failed");
          return;
        }

        // Still in progress -- continue polling
        pollTimerRef.current = setTimeout(() => {
          pollIaCTaskRef.current?.(taskId, attempt + 1);
        }, IAC_POLL_INTERVAL_MS);
      } catch (err) {
        const errMsg = err instanceof Error ? err.message : "Failed to check IaC status";
        onIaCFailedRef.current?.(errMsg);
      }
    },
    [tenantId],
  );
  useEffect(() => { pollIaCTaskRef.current = pollIaCTask; });

  // Start polling when we have an active task and no WebSocket.
  // Also cancel polling when WebSocket connects (ref sync above handles checks).
  useEffect(() => {
    if (iacTaskId && isTaskActive && !wsConnectedRef.current) {
      pollIaCTask(iacTaskId);
    }

    return () => {
      if (pollTimerRef.current) {
        clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [iacTaskId, isTaskActive, pollIaCTask]);

  // Cancel polling when WebSocket connects
  useEffect(() => {
    wsConnectedRef.current = wsConnected;
    if (wsConnected && pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, [wsConnected]);

  // ---------------------------------------------------------------------------
  // Show loading state when task is active
  // ---------------------------------------------------------------------------
  if (isTaskActive) {
    return (
      <StepContainer
        title="Infrastructure as Code"
        description="Generating validated CloudFormation templates from your approved design."
        error={error}
      >
        <IaCLoading taskStatus={iacTaskStatus ?? null} taskId={iacTaskId} />
      </StepContainer>
    );
  }

  // ---------------------------------------------------------------------------
  // No IaC output yet -- nothing to show (or task failed)
  // ---------------------------------------------------------------------------
  if (!iac || fileCount === 0) {
    const hasFailed = !!error;
    return (
      <StepContainer
        title="Infrastructure as Code"
        description={hasFailed ? "Infrastructure code generation failed." : "No infrastructure code generated yet."}
        onBack={onBack}
        error={error}
      >
        <div className="text-center py-8">
          {hasFailed ? (
            <div className="space-y-4">
              <div className="flex justify-center">
                <div className="h-12 w-12 rounded-full bg-red-50 flex items-center justify-center">
                  <svg className="h-6 w-6 text-red-500" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                  </svg>
                </div>
              </div>
              <p className="text-sm text-gray-600">
                Generation encountered an error. You can retry without losing your deployment parameters.
              </p>
              {onRegenerateIaC && (
                <button
                  onClick={() => onRegenerateIaC("")}
                  disabled={loading}
                  className="inline-flex items-center gap-2 px-5 py-2.5 text-sm font-medium bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                >
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182" />
                  </svg>
                  {loading ? "Retrying..." : "Retry Generation"}
                </button>
              )}
            </div>
          ) : (
            <p className="text-gray-500">Infrastructure code will appear here once generation is complete.</p>
          )}
        </div>
      </StepContainer>
    );
  }

  // ---------------------------------------------------------------------------
  // Completed state — show file browser, validation, download
  // ---------------------------------------------------------------------------
  return (
    <StepContainer
      title="Infrastructure as Code"
      description="Validated CloudFormation templates generated from your approved design."
      onNext={isComplete ? onContinue : undefined}
      onBack={isComplete ? onBack : undefined}
      nextLabel={loading ? "Generating Docs..." : "Generate Documentation"}
      loading={loading}
      error={error}
    >
      <div>
        {/* Generation metadata */}
        {iac.template_resolution_path && (
          <div className="mb-4 flex items-center gap-4 text-xs text-gray-500">
            <span>
              Generation path:{" "}
              <span className="font-medium text-gray-700">
                {resolutionPathLabel(iac.template_resolution_path)}
              </span>
            </span>
            {iac.generation_duration_ms > 0 && (
              <span>
                Duration:{" "}
                <span className="font-medium text-gray-700">
                  {(iac.generation_duration_ms / 1000).toFixed(1)}s
                </span>
              </span>
            )}
          </div>
        )}

        {/* File browser */}
        <div className="flex gap-4">
          {/* Sidebar — file groups */}
          <div className="w-56 shrink-0 border-r border-gray-200 pr-4 space-y-3">
            {groups.map((group) => (
              <div key={group.label}>
                <h4 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1">
                  {group.label}
                </h4>
                {group.files.map((f) => (
                  <button
                    key={f.path}
                    onClick={() => setActiveFile(f.path)}
                    className={`block w-full text-left px-2 py-1 text-sm rounded ${
                      selectedFile === f.path
                        ? "bg-blue-100 text-blue-800 font-medium"
                        : "text-gray-600 hover:bg-gray-100"
                    }`}
                  >
                    {f.name}
                  </button>
                ))}
              </div>
            ))}
          </div>

          {/* Code pane */}
          <div className="flex-1 min-w-0">
            {selectedFile && iac.files[selectedFile] && (() => {
              const code = iac.files[selectedFile];
              const lineCount = code.split("\n").length;
              const PREVIEW_LINES = 30;
              const isLong = lineCount > PREVIEW_LINES;
              const isExpanded = expandedFiles.has(selectedFile);
              const showFull = !isLong || isExpanded;

              const displayCode = showFull
                ? code
                : code.split("\n").slice(0, PREVIEW_LINES).join("\n");

              return (
                <>
                  <div className="flex items-center justify-between mb-2">
                    <p className="text-xs text-gray-400 font-mono">
                      {selectedFile}
                      <span className="ml-2 text-gray-300">{lineCount} lines</span>
                    </p>
                    {isLong && (
                      <button
                        onClick={() => {
                          setExpandedFiles((prev) => {
                            const next = new Set(prev);
                            if (next.has(selectedFile)) {
                              next.delete(selectedFile);
                            } else {
                              next.add(selectedFile);
                            }
                            return next;
                          });
                        }}
                        className="text-xs text-blue-600 hover:text-blue-800 font-medium"
                      >
                        {isExpanded ? "Collapse" : "View entire file"}
                      </button>
                    )}
                  </div>
                  <div className="relative">
                    <CodeBlock
                      code={displayCode}
                      language={langForFile(selectedFile)}
                    />
                    {isLong && !isExpanded && (
                      <div className="absolute bottom-0 left-0 right-0 h-16 bg-linear-to-t from-white to-transparent pointer-events-none rounded-b-lg" />
                    )}
                  </div>
                </>
              );
            })()}
          </div>
        </div>

        {/* Validation Report */}
        {iac.validation_report && (
          <div className="mt-4">
            <button
              onClick={() => setShowValidation(!showValidation)}
              className="flex items-center gap-1 text-sm font-medium text-gray-600 hover:text-gray-900"
            >
              <span
                className={`transition-transform ${showValidation ? "rotate-90" : ""}`}
              >
                &#9654;
              </span>
              Validation Report
              <span className={`ml-2 text-xs px-1.5 py-0.5 rounded ${
                iac.validation_report.passed
                  ? "bg-green-100 text-green-700"
                  : "bg-red-100 text-red-700"
              }`}>
                {iac.validation_report.passed ? "Passed" : "Issues Found"}
              </span>
            </button>
            {showValidation && (
              <div className="mt-3 p-4 bg-gray-50 rounded-lg">
                <ValidationReportView report={iac.validation_report} />
              </div>
            )}
          </div>
        )}

        {/* Download button */}
        {projectId && (
          <div className="mt-4">
            <a
              href={`${API}/api/export/${encodeURIComponent(projectId)}/iac.zip?tenant_id=${encodeURIComponent(tenantId)}`}
              download
              className="inline-flex items-center gap-2 px-4 py-2 bg-green-600 text-white text-sm font-medium rounded-lg hover:bg-green-700 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
              Download All Files (.zip)
            </a>
          </div>
        )}

        {/* Regenerate with feedback */}
        {onRegenerateIaC && (
          <div className="mt-6 pt-5 border-t border-gray-200">
            {!showFeedback ? (
              <button
                onClick={() => setShowFeedback(true)}
                disabled={loading}
                className="text-sm text-gray-500 hover:text-gray-700 underline disabled:opacity-50"
              >
                Not what you expected? Regenerate with feedback
              </button>
            ) : (
              <div className="space-y-3">
                <label className="block text-sm font-medium text-gray-700">
                  What changes would you like to see in the generated IaC?
                </label>
                <textarea
                  value={feedback}
                  onChange={(e) => setFeedback(e.target.value)}
                  rows={3}
                  placeholder="e.g., Change subnets to /24, use GWLB instead of TGW, add a second AZ..."
                  className="w-full px-3 py-2 bg-white text-gray-900 border border-gray-300 rounded-lg text-sm outline-none focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 placeholder:text-gray-400"
                />
                <div className="flex gap-3">
                  <button
                    onClick={handleRegenerateIaC}
                    disabled={loading || !feedback.trim()}
                    className="px-4 py-2 text-sm font-medium bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                  >
                    {loading ? "Regenerating IaC..." : "Regenerate IaC"}
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
      </div>
    </StepContainer>
  );
}
