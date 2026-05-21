"use client";

import { useReducer, useCallback, useRef, useEffect } from "react";
import type {
  WizardStep,
  WizardState,
  RequirementsSeed,
  InterviewOutput,
  DesignRecommendation,
  DesignTaskResponse,
  IaCTaskStatus,
  DocsTaskStatus,
  RefinementPlan,
  DeploymentParameters,
  IaCOutput,
  DocumentationOutput,
} from "@/lib/types";
import {
  submitDesign,
  getDesignTask,
  selectDesign,
  refineDesign,
  submitIaCTask,
  submitDocsTask,
  getDocsTask,
  regenerateDocsSection,
  getProjectState,
} from "@/lib/api";
import { usePollTask } from "@/hooks/usePollTask";
import { useWebSocket } from "@/hooks/useWebSocket";

const DESIGN_POLL_INTERVAL_MS = 3_000;
const DESIGN_POLL_MAX_ATTEMPTS = 60;
const DOCS_POLL_INTERVAL_MS = 3_000;
const DOCS_POLL_MAX_ATTEMPTS = 120; // 6 minutes max — docs generation is slower

const INITIAL_STATE: WizardState = {
  step: "requirements",
  requirementsSeed: null,
  requirements: null,
  showInterviewChat: false,
  recommendation: null,
  approvedDesignIndex: null,
  refinementPlan: null,
  deploymentParameters: null,
  designTaskId: null,
  designTaskStatus: null,
  iac: null,
  iacTaskId: null,
  iacTaskStatus: null,
  docs: null,
  docsTaskId: null,
  docsTaskStatus: null,
  regeneratingSection: null,
  loading: false,
  error: null,
  hydrating: true,
};

// ---------------------------------------------------------------------------
// Action types -- discriminated union covering all state transitions
// ---------------------------------------------------------------------------

type WizardAction =
  | { type: "HYDRATE"; partial: Partial<WizardState> }
  | { type: "SET_LOADING"; loading: boolean }
  | { type: "SET_ERROR"; error: string | null }
  | { type: "SUBMIT_SEED"; seed: RequirementsSeed }
  | { type: "INTERVIEW_COMPLETE"; requirements: InterviewOutput }
  | { type: "PROCEED_TO_DESIGN_START" }
  | { type: "PROCEED_TO_DESIGN_SUCCESS"; recommendation: DesignRecommendation }
  | { type: "DESIGN_SUBMITTED"; taskId: string; status: string }
  | { type: "DESIGN_TASK_UPDATE"; status: string; recommendation?: DesignRecommendation; error?: string }
  | { type: "DESIGN_SELECT_SUCCESS"; index: number; refinementPlan: RefinementPlan }
  | { type: "REFINE_SUCCESS"; deploymentParameters: DeploymentParameters }
  | { type: "APPROVE_DESIGN_START"; index: number }
  | { type: "IAC_SUBMITTED"; taskId: string; status: IaCTaskStatus }
  | { type: "IAC_TASK_UPDATE"; status: IaCTaskStatus; iac?: IaCOutput; error?: string }
  | { type: "SET_IAC_COMPLETE"; iac: IaCOutput }
  | { type: "REDESIGN_START" }
  | { type: "REDESIGN_SUCCESS"; recommendation: DesignRecommendation }
  | { type: "GENERATE_DOCS_START" }
  | { type: "DOCS_SUBMITTED"; taskId: string; status: DocsTaskStatus }
  | { type: "DOCS_TASK_UPDATE"; status: DocsTaskStatus; docs?: DocumentationOutput; error?: string }
  | { type: "DOCS_SECTION_READY"; section: keyof DocumentationOutput; content: string }
  | { type: "DOCS_SECTION_REGENERATING"; section: keyof DocumentationOutput }
  | { type: "DOCS_SECTION_REGEN_FAILED"; error: string }
  | { type: "STREAM_ERROR"; error: string }
  | { type: "GO_BACK" }
  | { type: "RESET" }
  | { type: "PATCH"; partial: Partial<WizardState> };

// ---------------------------------------------------------------------------
// Reducer -- pure function handling all state transitions
// ---------------------------------------------------------------------------

function wizardReducer(state: WizardState, action: WizardAction): WizardState {
  switch (action.type) {
    case "HYDRATE":
      return { ...state, ...action.partial };

    case "SET_LOADING":
      return { ...state, loading: action.loading };

    case "SET_ERROR":
      return { ...state, error: action.error };

    case "SUBMIT_SEED":
      return {
        ...state,
        requirementsSeed: action.seed,
        showInterviewChat: true,
        error: null,
      };

    case "INTERVIEW_COMPLETE":
      return {
        ...state,
        requirements: action.requirements,
      };

    case "PROCEED_TO_DESIGN_START":
      return {
        ...state,
        loading: true,
        error: null,
        showInterviewChat: false,
      };

    case "PROCEED_TO_DESIGN_SUCCESS":
      return {
        ...state,
        recommendation: action.recommendation,
        step: "design",
        loading: false,
        designTaskId: null,
        designTaskStatus: null,
      };

    case "DESIGN_SUBMITTED":
      return {
        ...state,
        designTaskId: action.taskId,
        designTaskStatus: action.status,
      };

    case "DESIGN_TASK_UPDATE": {
      if (action.status === "completed" && action.recommendation) {
        return {
          ...state,
          recommendation: action.recommendation,
          step: "design",
          loading: false,
          designTaskId: null,
          designTaskStatus: null,
        };
      }
      if (action.status === "failed") {
        return {
          ...state,
          loading: false,
          designTaskId: null,
          designTaskStatus: null,
          error: action.error ?? "Design task failed",
        };
      }
      return {
        ...state,
        designTaskStatus: action.status,
      };
    }

    case "DESIGN_SELECT_SUCCESS":
      return {
        ...state,
        approvedDesignIndex: action.index,
        refinementPlan: action.refinementPlan,
        loading: false,
      };

    case "REFINE_SUCCESS":
      return {
        ...state,
        deploymentParameters: action.deploymentParameters,
        loading: false,
        step: "iac",
        iac: null,
      };

    case "APPROVE_DESIGN_START":
      return {
        ...state,
        loading: true,
        error: null,
        approvedDesignIndex: action.index,
        step: "iac",
        iac: null,
      };

    case "IAC_SUBMITTED":
      return {
        ...state,
        iacTaskId: action.taskId,
        iacTaskStatus: action.status,
        loading: true,
      };

    case "IAC_TASK_UPDATE": {
      if (action.status === "completed" && action.iac) {
        return {
          ...state,
          iac: action.iac,
          loading: false,
          iacTaskId: null,
          iacTaskStatus: null,
        };
      }
      if (action.status === "failed") {
        return {
          ...state,
          loading: false,
          iacTaskId: null,
          iacTaskStatus: null,
          error: action.error ?? "IaC task failed",
        };
      }
      return {
        ...state,
        iacTaskStatus: action.status,
      };
    }

    case "SET_IAC_COMPLETE":
      return {
        ...state,
        iac: action.iac,
        loading: false,
        iacTaskId: null,
        iacTaskStatus: null,
      };

    case "REDESIGN_START":
      return { ...state, loading: true, error: null };

    case "REDESIGN_SUCCESS":
      return {
        ...state,
        recommendation: action.recommendation,
        loading: false,
        refinementPlan: null,
        deploymentParameters: null,
        approvedDesignIndex: null,
      };

    case "GENERATE_DOCS_START":
      return {
        ...state,
        loading: true,
        error: null,
        step: "documentation",
        docs: null,
        docsTaskId: null,
        docsTaskStatus: null,
      };

    case "DOCS_SUBMITTED":
      return {
        ...state,
        docsTaskId: action.taskId,
        docsTaskStatus: action.status,
      };

    case "DOCS_TASK_UPDATE": {
      if (action.status === "completed" && action.docs) {
        return {
          ...state,
          docs: action.docs,
          loading: false,
          docsTaskId: null,
          docsTaskStatus: null,
        };
      }
      if (action.status === "failed") {
        return {
          ...state,
          loading: false,
          docsTaskId: null,
          docsTaskStatus: null,
          error: action.error ?? "Documentation generation failed",
        };
      }
      return {
        ...state,
        docsTaskStatus: action.status,
      };
    }

    case "DOCS_SECTION_READY": {
      const currentDocs = state.docs ?? { user_guide: "", architecture_diagram: "" };
      return {
        ...state,
        docs: { ...currentDocs, [action.section]: action.content },
        regeneratingSection: state.regeneratingSection === action.section ? null : state.regeneratingSection,
      };
    }

    case "DOCS_SECTION_REGENERATING":
      return {
        ...state,
        regeneratingSection: action.section,
        error: null,
      };

    case "DOCS_SECTION_REGEN_FAILED":
      return {
        ...state,
        regeneratingSection: null,
        error: action.error,
      };

    case "STREAM_ERROR":
      return {
        ...state,
        loading: false,
        iacTaskId: null,
        iacTaskStatus: null,
        docsTaskId: null,
        docsTaskStatus: null,
        error: action.error,
      };

    case "GO_BACK": {
      const order: WizardStep[] = [
        "requirements",
        "design",
        "iac",
        "documentation",
      ];
      const idx = order.indexOf(state.step);
      if (idx > 0) {
        return {
          ...state,
          step: order[idx - 1],
          error: null,
          iacTaskId: null,
          iacTaskStatus: null,
          docsTaskId: null,
          docsTaskStatus: null,
        };
      }
      return state;
    }

    case "RESET":
      return { ...INITIAL_STATE, hydrating: false };

    case "PATCH":
      return { ...state, ...action.partial };

    default:
      return state;
  }
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

interface UseWizardStateOptions {
  onDesignSubmitted?: () => void;
  onIaCSubmitted?: () => void;
}

export function useWizardState(
  projectId: string,
  tenantId = "default",
  options?: UseWizardStateOptions,
) {
  const onDesignSubmittedRef = useRef(options?.onDesignSubmitted);
  const onIaCSubmittedRef = useRef(options?.onIaCSubmitted);
  useEffect(() => {
    onDesignSubmittedRef.current = options?.onDesignSubmitted;
    onIaCSubmittedRef.current = options?.onIaCSubmitted;
  });
  const [state, dispatch] = useReducer(wizardReducer, INITIAL_STATE);

  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  const wsConnectedRef = useRef(false);

  // ---------------------------------------------------------------------------
  // Design task polling (via usePollTask)
  // ---------------------------------------------------------------------------

  const { startPolling: pollDesignTask, stopPolling: stopDesignPolling } = usePollTask<DesignTaskResponse>({
    fetchFn: (taskId) => getDesignTask(taskId, tenantId),
    isComplete: (task) => task.status === "completed" && !!task.result,
    isFailed: (task) => task.status === "failed",
    onComplete: (task) => {
      dispatch({ type: "DESIGN_TASK_UPDATE", status: "completed", recommendation: task.result });
    },
    onFailed: (task) => {
      dispatch({ type: "DESIGN_TASK_UPDATE", status: "failed", error: task.error ?? "Design task failed" });
    },
    onProgress: (task) => {
      dispatch({ type: "DESIGN_TASK_UPDATE", status: task.status });
    },
    onTimeout: () => {
      dispatch({ type: "DESIGN_TASK_UPDATE", status: "failed", error: "Design task timed out. Please try again." });
    },
    onError: (error) => {
      dispatch({ type: "DESIGN_TASK_UPDATE", status: "failed", error });
    },
    isWsConnected: () => wsConnectedRef.current,
    intervalMs: DESIGN_POLL_INTERVAL_MS,
    maxAttempts: DESIGN_POLL_MAX_ATTEMPTS,
  });

  // ---------------------------------------------------------------------------
  // Docs task polling (via usePollTask)
  // ---------------------------------------------------------------------------

  const { startPolling: pollDocsTask, stopPolling: stopDocsPolling } = usePollTask<{ status: string; result?: unknown; error?: string }>({
    fetchFn: (taskId) => getDocsTask(taskId, tenantId),
    isComplete: (task) => task.status === "completed" && !!task.result,
    isFailed: (task) => task.status === "failed",
    onComplete: (task) => {
      dispatch({ type: "DOCS_TASK_UPDATE", status: "completed", docs: task.result as DocumentationOutput });
    },
    onFailed: (task) => {
      dispatch({ type: "DOCS_TASK_UPDATE", status: "failed", error: task.error ?? "Documentation task failed" });
    },
    onProgress: (task) => {
      dispatch({ type: "DOCS_TASK_UPDATE", status: task.status as DocsTaskStatus });
    },
    onTimeout: () => {
      dispatch({ type: "DOCS_TASK_UPDATE", status: "failed", error: "Documentation task timed out. Please try again." });
    },
    onError: (error) => {
      dispatch({ type: "DOCS_TASK_UPDATE", status: "failed", error });
    },
    isWsConnected: () => wsConnectedRef.current,
    intervalMs: DOCS_POLL_INTERVAL_MS,
    maxAttempts: DOCS_POLL_MAX_ATTEMPTS,
  });

  // ---------------------------------------------------------------------------
  // WebSocket for real-time design task updates
  // ---------------------------------------------------------------------------

  const handleDesignComplete = useCallback(
    async (result: unknown) => {
      stopDesignPolling();
      if (result) {
        dispatch({
          type: "DESIGN_TASK_UPDATE",
          status: "completed",
          recommendation: result as DesignRecommendation,
        });
        return;
      }
      const taskId = stateRef.current.designTaskId;
      if (!taskId) return;
      try {
        const task = await getDesignTask(taskId, tenantId);
        if (task.result) {
          dispatch({
            type: "DESIGN_TASK_UPDATE",
            status: "completed",
            recommendation: task.result,
          });
        }
      } catch {
        pollDesignTask(taskId);
      }
    },
    [stopDesignPolling, tenantId, pollDesignTask],
  );

  const handleDesignFailed = useCallback(
    (error: string) => {
      stopDesignPolling();
      dispatch({
        type: "DESIGN_TASK_UPDATE",
        status: "failed",
        error,
      });
    },
    [stopDesignPolling],
  );

  // ---------------------------------------------------------------------------
  // WebSocket callbacks for IaC task updates
  // ---------------------------------------------------------------------------

  const handleIaCStatus = useCallback(
    (status: string) => {
      dispatch({
        type: "IAC_TASK_UPDATE",
        status: status as IaCTaskStatus,
      });
    },
    [],
  );

  const handleIaCComplete = useCallback(
    (result: unknown) => {
      dispatch({
        type: "IAC_TASK_UPDATE",
        status: "completed",
        iac: result as IaCOutput,
      });
    },
    [],
  );

  const handleIaCFailed = useCallback(
    (error: string) => {
      dispatch({
        type: "IAC_TASK_UPDATE",
        status: "failed",
        error,
      });
    },
    [],
  );

  // ---------------------------------------------------------------------------
  // WebSocket callbacks for Docs task updates
  // ---------------------------------------------------------------------------

  const handleDocsStatus = useCallback(
    (status: string) => {
      dispatch({
        type: "DOCS_TASK_UPDATE",
        status: status as DocsTaskStatus,
      });
    },
    [],
  );

  const handleDocsSection = useCallback(
    (section: string, content: string) => {
      const validSections: Array<keyof DocumentationOutput> = [
        "user_guide", "architecture_diagram",
      ];
      if (validSections.includes(section as keyof DocumentationOutput)) {
        dispatch({
          type: "DOCS_SECTION_READY",
          section: section as keyof DocumentationOutput,
          content,
        });
      }
    },
    [],
  );

  const handleDocsComplete = useCallback(
    (result: unknown) => {
      stopDocsPolling();
      dispatch({
        type: "DOCS_TASK_UPDATE",
        status: "completed",
        docs: result as DocumentationOutput,
      });
    },
    [stopDocsPolling],
  );

  const handleDocsFailed = useCallback(
    (error: string) => {
      stopDocsPolling();
      dispatch({
        type: "DOCS_TASK_UPDATE",
        status: "failed",
        error,
      });
    },
    [stopDocsPolling],
  );

  const { connected: wsConnected } = useWebSocket({
    projectId,
    tenantId,
    onDesignComplete: handleDesignComplete,
    onDesignFailed: handleDesignFailed,
    onIaCStatus: handleIaCStatus,
    onIaCComplete: handleIaCComplete,
    onIaCFailed: handleIaCFailed,
    onDocsStatus: handleDocsStatus,
    onDocsSection: handleDocsSection,
    onDocsComplete: handleDocsComplete,
    onDocsFailed: handleDocsFailed,
  });

  // Keep ref in sync so polling can read the latest value.
  // Also cancel active polling whenever WebSocket connects — it takes over.
  useEffect(() => {
    wsConnectedRef.current = wsConnected;
    if (wsConnected) {
      stopDesignPolling();
      stopDocsPolling();
    }
  }, [wsConnected, stopDesignPolling, stopDocsPolling]);


  // ---------------------------------------------------------------------------
  // Hydrate from backend on mount
  // ---------------------------------------------------------------------------

  function validateStepPrerequisites(
    step: WizardStep,
    data: {
      requirements: unknown;
      design: unknown;
      iac: unknown;
      docs: unknown;
      approvedDesignIndex: number | null | undefined;
      hasActiveIaCTask: boolean;
      hasActiveDocsTask: boolean;
    },
  ): WizardStep {
    // Don't land on documentation without docs unless there's an active task
    if (step === "documentation" && !data.docs && !data.hasActiveDocsTask) return data.iac ? "iac" : "design";
    if (step === "documentation" && !data.iac) return "iac";
    // Allow iac step if there's an active IaC task (even without result yet)
    if (step === "iac" && data.approvedDesignIndex == null && !data.hasActiveIaCTask) return "design";
    if (step === "iac" && !data.design) return "design";
    if (step === "design" && !data.requirements) return "requirements";
    return step;
  }

  useEffect(() => {
    let cancelled = false;

    async function hydrate() {
      try {
        const saved = await getProjectState(projectId, tenantId);
        if (cancelled) return;

        // Determine which step to show based on saved data
        const project = saved.project;
        const rawStep: WizardStep = project.current_step as WizardStep;

        // Detect active tasks
        const hasActiveDesignTask = !!project.active_design_task_id && !saved.design;
        const hasActiveIaCTask = !!project.active_iac_task_id && !saved.iac;
        const hasActiveDocsTask = !!project.active_docs_task_id && !saved.docs;

        // Validate step has required prerequisite data
        const step = validateStepPrerequisites(rawStep, {
          requirements: saved.requirements,
          design: saved.design,
          iac: saved.iac,
          docs: saved.docs,
          approvedDesignIndex: project.approved_design_index,
          hasActiveIaCTask,
          hasActiveDocsTask,
        });

        // Determine which step to show based on active tasks
        const resolvedStep = hasActiveDesignTask
          ? "requirements"
          : hasActiveIaCTask
            ? "iac"
            : hasActiveDocsTask
              ? "documentation"
              : step;

        dispatch({
          type: "HYDRATE",
          partial: {
            hydrating: false,
            step: resolvedStep,
            requirements: saved.requirements ?? null,
            recommendation: saved.design ?? null,
            approvedDesignIndex: project.approved_design_index ?? null,
            iac: saved.iac ?? null,
            docs: saved.docs ?? null,
            ...(hasActiveDesignTask
              ? {
                  designTaskId: project.active_design_task_id,
                  designTaskStatus: "processing",
                  loading: true,
                  showInterviewChat: false,
                }
              : {}),
            ...(hasActiveIaCTask
              ? {
                  iacTaskId: project.active_iac_task_id,
                  iacTaskStatus: "processing" as IaCTaskStatus,
                  loading: true,
                }
              : {}),
            ...(hasActiveDocsTask
              ? {
                  docsTaskId: project.active_docs_task_id,
                  docsTaskStatus: "processing" as DocsTaskStatus,
                  loading: true,
                }
              : {}),
          },
        });

      } catch {
        // Project not found or API unavailable -- start fresh
        if (!cancelled) {
          dispatch({ type: "HYDRATE", partial: { hydrating: false } });
        }
      }
    }

    hydrate();
    return () => {
      cancelled = true;
    };
  }, [projectId, tenantId]);

  // Resume polling for an active design task discovered during hydration.
  // Runs once after hydration completes if there's a task still in progress.
  useEffect(() => {
    const { hydrating, designTaskId } = stateRef.current;
    if (hydrating || !designTaskId) return;
    // WS will pick it up if connected; poll as fallback
    if (!wsConnectedRef.current) {
      pollDesignTask(designTaskId);
    }
  }, [state.hydrating, state.designTaskId, pollDesignTask]);

  // Resume polling for an active docs task discovered during hydration.
  useEffect(() => {
    const { hydrating, docsTaskId } = stateRef.current;
    if (hydrating || !docsTaskId) return;
    if (!wsConnectedRef.current) {
      pollDocsTask(docsTaskId);
    }
  }, [state.hydrating, state.docsTaskId, pollDocsTask]);

  const submitRequirements = useCallback((data: RequirementsSeed) => {
    dispatch({ type: "SUBMIT_SEED", seed: data });
  }, []);

  const interviewComplete = useCallback((requirements: InterviewOutput) => {
    dispatch({ type: "INTERVIEW_COMPLETE", requirements });
  }, []);

  const proceedToDesign = useCallback(async () => {
    const { requirements } = stateRef.current;
    if (!requirements) return;

    dispatch({ type: "PROCEED_TO_DESIGN_START" });

    try {
      const taskResponse = await submitDesign(requirements, tenantId, projectId);

      dispatch({
        type: "DESIGN_SUBMITTED",
        taskId: taskResponse.task_id,
        status: taskResponse.status,
      });

      // If the task completed immediately (synchronous response)
      if (taskResponse.status === "completed" && taskResponse.result) {
        dispatch({
          type: "PROCEED_TO_DESIGN_SUCCESS",
          recommendation: taskResponse.result,
        });
        return;
      }

      // If the task failed immediately
      if (taskResponse.status === "failed") {
        dispatch({
          type: "DESIGN_TASK_UPDATE",
          status: "failed",
          error: taskResponse.error ?? "Design task failed",
        });
        return;
      }

      // Navigate away — user can return later to see results
      onDesignSubmittedRef.current?.();

      // Start polling as fallback — it self-terminates if WebSocket connects
      if (!wsConnectedRef.current) {
        pollDesignTask(taskResponse.task_id);
      }
    } catch (err) {
      const error = err instanceof Error ? err.message : "Design generation failed";
      dispatch({ type: "PATCH", partial: { loading: false, error } });
    }
  }, [tenantId, projectId, pollDesignTask]);

  const approveDesign = useCallback(async (index: number) => {
    const { recommendation } = stateRef.current;
    if (!recommendation) return;

    if (index < 0 || index >= recommendation.options.length) {
      dispatch({ type: "SET_ERROR", error: "Invalid design selection." });
      return;
    }

    dispatch({ type: "SET_LOADING", loading: true });
    dispatch({ type: "SET_ERROR", error: null });

    try {
      const { refinement_plan } = await selectDesign(projectId, index, tenantId);
      dispatch({
        type: "DESIGN_SELECT_SUCCESS",
        index,
        refinementPlan: refinement_plan,
      });
    } catch (err) {
      const error = err instanceof Error ? err.message : "Design selection failed";
      dispatch({ type: "PATCH", partial: { loading: false, error } });
    }
  }, [projectId, tenantId]);

  const submitDeploymentParams = useCallback(async (params: DeploymentParameters) => {
    const { recommendation, approvedDesignIndex, requirements } = stateRef.current;
    if (!recommendation || approvedDesignIndex === null || !requirements) return;

    if (approvedDesignIndex < 0 || approvedDesignIndex >= recommendation.options.length) {
      dispatch({ type: "SET_ERROR", error: "Invalid design selection." });
      return;
    }

    dispatch({ type: "SET_LOADING", loading: true });
    dispatch({ type: "SET_ERROR", error: null });

    try {
      await refineDesign(projectId, params, tenantId);

      dispatch({
        type: "REFINE_SUCCESS",
        deploymentParameters: params,
      });

      // Submit async IaC generation task
      try {
        const taskResponse = await submitIaCTask(projectId, tenantId);

        dispatch({
          type: "IAC_SUBMITTED",
          taskId: taskResponse.task_id,
          status: taskResponse.status as IaCTaskStatus,
        });

        // If the task completed immediately (unlikely but possible)
        if (taskResponse.status === "completed" && taskResponse.result) {
          dispatch({ type: "SET_IAC_COMPLETE", iac: taskResponse.result });
          return;
        }

        if (taskResponse.status === "failed") {
          dispatch({
            type: "IAC_TASK_UPDATE",
            status: "failed",
            error: taskResponse.error ?? "IaC task failed",
          });
          return;
        }

        // Navigate away — user can return later to see results
        onIaCSubmittedRef.current?.();

        // Polling is handled by IaCView when WebSocket is unavailable
      } catch (err) {
        const error = err instanceof Error ? err.message : "IaC generation failed";
        dispatch({ type: "STREAM_ERROR", error });
      }
    } catch (err) {
      const error = err instanceof Error ? err.message : "Parameter refinement failed";
      dispatch({ type: "PATCH", partial: { loading: false, error } });
    }
  }, [projectId, tenantId]);

  const requestRedesign = useCallback(async (feedback: string) => {
    const { recommendation, requirements } = stateRef.current;
    if (!requirements) return;

    dispatch({ type: "REDESIGN_START" });

    try {
      const previousOptions = recommendation?.options ?? [];
      const taskResponse = await submitDesign(
        requirements,
        tenantId,
        projectId,
        feedback,
        previousOptions,
      );

      dispatch({
        type: "DESIGN_SUBMITTED",
        taskId: taskResponse.task_id,
        status: taskResponse.status,
      });

      // If the task completed immediately
      if (taskResponse.status === "completed" && taskResponse.result) {
        dispatch({ type: "REDESIGN_SUCCESS", recommendation: taskResponse.result });
        return;
      }

      if (taskResponse.status === "failed") {
        dispatch({
          type: "DESIGN_TASK_UPDATE",
          status: "failed",
          error: taskResponse.error ?? "Redesign task failed",
        });
        return;
      }

      // Start polling as fallback — it self-terminates if WebSocket connects
      if (!wsConnectedRef.current) {
        pollDesignTask(taskResponse.task_id);
      }
    } catch (err) {
      const error = err instanceof Error ? err.message : "Redesign generation failed";
      dispatch({ type: "PATCH", partial: { loading: false, error } });
    }
  }, [tenantId, projectId, pollDesignTask]);

  const regenerateIaC = useCallback(async (feedback: string) => {
    dispatch({ type: "SET_LOADING", loading: true });
    dispatch({ type: "SET_ERROR", error: null });
    // Clear old IaC so loading state shows
    dispatch({ type: "PATCH", partial: { iac: null } });

    try {
      const taskResponse = await submitIaCTask(projectId, tenantId, feedback);

      dispatch({
        type: "IAC_SUBMITTED",
        taskId: taskResponse.task_id,
        status: taskResponse.status as IaCTaskStatus,
      });

      if (taskResponse.status === "completed" && taskResponse.result) {
        dispatch({ type: "SET_IAC_COMPLETE", iac: taskResponse.result });
        return;
      }

      if (taskResponse.status === "failed") {
        dispatch({
          type: "IAC_TASK_UPDATE",
          status: "failed",
          error: taskResponse.error ?? "IaC regeneration failed",
        });
        return;
      }
      // No redirect for regeneration — user stays on IaC step to see results
    } catch (err) {
      const error = err instanceof Error ? err.message : "IaC regeneration failed";
      dispatch({ type: "PATCH", partial: { loading: false, error } });
    }
  }, [projectId, tenantId]);

  const generateDocs = useCallback(async () => {
    dispatch({ type: "GENERATE_DOCS_START" });

    try {
      const taskResponse = await submitDocsTask(tenantId, projectId);

      dispatch({
        type: "DOCS_SUBMITTED",
        taskId: taskResponse.task_id,
        status: taskResponse.status as DocsTaskStatus,
      });

      // If the task completed immediately (unlikely but possible)
      if (taskResponse.status === "completed" && taskResponse.result) {
        dispatch({
          type: "DOCS_TASK_UPDATE",
          status: "completed",
          docs: taskResponse.result as DocumentationOutput,
        });
        return;
      }

      if (taskResponse.status === "failed") {
        dispatch({
          type: "DOCS_TASK_UPDATE",
          status: "failed",
          error: taskResponse.error ?? "Documentation task failed",
        });
        return;
      }

      // Start polling as fallback — self-terminates if WebSocket connects
      if (!wsConnectedRef.current) {
        pollDocsTask(taskResponse.task_id);
      }
    } catch (err) {
      const error = err instanceof Error ? err.message : "Documentation generation failed";
      dispatch({ type: "STREAM_ERROR", error });
    }
  }, [tenantId, projectId, pollDocsTask]);

  const regenerateSection = useCallback(
    async (section: keyof DocumentationOutput) => {
      dispatch({ type: "DOCS_SECTION_REGENERATING", section });
      try {
        const result = await regenerateDocsSection(projectId, section, tenantId);
        dispatch({
          type: "DOCS_SECTION_READY",
          section: result.section as keyof DocumentationOutput,
          content: result.content,
        });
      } catch (err) {
        const error = err instanceof Error ? err.message : "Failed to regenerate section";
        dispatch({ type: "DOCS_SECTION_REGEN_FAILED", error });
      }
    },
    [projectId, tenantId],
  );

  const goBack = useCallback(() => {
    dispatch({ type: "GO_BACK" });
  }, []);

  const reset = useCallback(() => {
    dispatch({ type: "RESET" });
  }, []);

  return {
    ...state,
    tenantId,
    wsConnected,
    submitRequirements,
    interviewComplete,
    proceedToDesign,
    approveDesign,
    submitDeploymentParams,
    requestRedesign,
    regenerateIaC,
    generateDocs,
    regenerateSection,
    goBack,
    reset,
    handleIaCComplete,
    handleIaCFailed,
    handleDocsComplete,
    handleDocsFailed,
  };
}
