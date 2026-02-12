import type {
  InterviewOutput,
  InputHint,
  DesignOption,
  DesignTaskResponse,
  RefinementPlan,
  DeploymentParameters,
  IaCTaskResponse,
  DocsTaskResponse,
  RegenerateDocsSectionResponse,
  Project,
  ProjectState,
} from "./types";

const API = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

function sanitizeApiError(text: string, status: number): string {
  if (status === 503) return "The AI service is temporarily unavailable. Please retry shortly.";
  if (status >= 500) return "An internal error occurred. Please try again.";
  if (status === 401) return "Authentication required.";
  if (status === 403) return "Permission denied.";
  if (status === 404) return "Resource not found.";
  if (status === 429) return "Too many requests. Please wait.";

  return text.length > 200 ? text.slice(0, 200) : text;
}

const REQUEST_TIMEOUT_MS = 30_000;
const AGENT_REQUEST_TIMEOUT_MS = 120_000;
const MAX_SSE_BUFFER = 10 * 1024 * 1024;

function withTenant(path: string, tenantId: string): string {
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}tenant_id=${encodeURIComponent(tenantId)}`;
}

async function request<T>(
  path: string,
  body?: unknown,
  timeoutMs = REQUEST_TIMEOUT_MS,
  method?: string,
): Promise<T> {
  let res: Response;
  const resolvedMethod = method ?? (body ? "POST" : "GET");
  try {
    res = await fetch(`${API}${path}`, {
      method: resolvedMethod,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "TimeoutError") {
      throw new Error("Request timed out. Please try again.");
    }
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error("Request was aborted.");
    }
    throw err;
  }
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`API ${res.status}: ${sanitizeApiError(text, res.status)}`);
  }
  const text = await res.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

function readSSE(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: (eventType: string, parsed: unknown) => void,
): Promise<void> {
  const decoder = new TextDecoder();
  let buffer = "";

  return (async () => {
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        if (buffer.length > MAX_SSE_BUFFER) {
          throw new Error("SSE response exceeded maximum buffer size.");
        }

        const parts = buffer.split("\n\n");
        buffer = parts.pop()!;

        for (const part of parts) {
          if (!part.trim()) continue;
          const lines = part.split("\n");
          let eventType = "";
          const dataLines: string[] = [];
          for (const line of lines) {
            if (line.startsWith(":")) continue; // SSE comment (e.g., heartbeat)
            if (line.startsWith("event: ")) eventType = line.slice(7);
            else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
          }
          if (dataLines.length === 0) continue;
          const data = dataLines.join("\n");

          let parsed;
          try {
            parsed = JSON.parse(data);
          } catch {
            continue;
          }
          onEvent(eventType, parsed);
        }
      }
    } finally {
      reader.cancel().catch(() => {});
    }
  })();
}

async function fetchSSEStream(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<ReadableStreamDefaultReader<Uint8Array>> {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`API ${res.status}: ${sanitizeApiError(text, res.status)}`);
  }

  if (!res.body) throw new Error("Response body is null");
  return res.body.getReader();
}

export function createProject(name: string, tenantId = "default"): Promise<Project> {
  return request(withTenant("/api/projects", tenantId), { name });
}

export function listProjects(tenantId = "default"): Promise<Project[]> {
  return request(withTenant("/api/projects", tenantId));
}

export function getProjectState(
  projectId: string,
  tenantId = "default",
): Promise<ProjectState> {
  return request(
    withTenant(`/api/projects/${encodeURIComponent(projectId)}/state`, tenantId),
  );
}

export function deleteProject(
  projectId: string,
  tenantId = "default",
): Promise<void> {
  return request(
    withTenant(`/api/projects/${encodeURIComponent(projectId)}`, tenantId),
    undefined,
    REQUEST_TIMEOUT_MS,
    "DELETE",
  );
}

export function submitDesign(
  requirements: InterviewOutput,
  tenantId = "default",
  projectId = "default",
  feedback?: string,
  previousOptions?: DesignOption[],
): Promise<DesignTaskResponse> {
  return request(withTenant("/api/design/submit", tenantId), {
    requirements,
    project_id: projectId,
    feedback: feedback ?? null,
    previous_options: previousOptions ?? null,
  }, AGENT_REQUEST_TIMEOUT_MS);
}

export function getDesignTask(
  taskId: string,
  tenantId = "default",
): Promise<DesignTaskResponse> {
  return request(withTenant(`/api/design/task/${encodeURIComponent(taskId)}`, tenantId));
}

export function selectDesign(
  projectId: string,
  optionIndex: number,
  tenantId = "default",
): Promise<{ selected_option: DesignOption; refinement_plan: RefinementPlan }> {
  return request(withTenant("/api/design/select", tenantId), {
    project_id: projectId,
    option_index: optionIndex,
  });
}

export function refineDesign(
  projectId: string,
  params: DeploymentParameters,
  tenantId = "default",
): Promise<{ resolved_parameters: Record<string, unknown> }> {
  return request(withTenant("/api/design/refine", tenantId), {
    project_id: projectId,
    ...params,
  });
}

// --- IaC: Async task submission + polling ---

export function submitIaCTask(
  projectId: string,
  tenantId = "default",
  feedback?: string,
): Promise<IaCTaskResponse> {
  return request(withTenant("/api/iac/submit", tenantId), {
    project_id: projectId,
    feedback: feedback ?? null,
  });
}

export function getIaCTask(
  taskId: string,
  tenantId = "default",
): Promise<IaCTaskResponse> {
  return request(
    withTenant(`/api/iac/task/${encodeURIComponent(taskId)}`, tenantId),
  );
}

export async function invokeInterviewChat(
  message: string,
  tenantId = "default",
  projectId = "default",
  requirements?: InterviewOutput,
  useCase?: string,
  seedData?: Record<string, unknown>,
  signal?: AbortSignal,
  populatedFields?: Record<string, unknown>,
): Promise<{ content: string; complete: boolean; requirements?: InterviewOutput; missingFields?: string[]; gatheredFields?: Record<string, unknown>; inputHint?: InputHint }> {
  const result = {
    content: "",
    complete: false,
    requirements: undefined as InterviewOutput | undefined,
    missingFields: undefined as string[] | undefined,
    gatheredFields: undefined as Record<string, unknown> | undefined,
    inputHint: undefined as InputHint | undefined,
  };

  const reqPayload = seedData ?? requirements ?? null;

  const reader = await fetchSSEStream(
    withTenant("/api/interview/chat", tenantId),
    {
      message,
      requirements: reqPayload,
      populated_fields: populatedFields ?? null,
      use_case: useCase ?? null,
      project_id: projectId,
    },
    signal,
  );

  try {
    await readSSE(reader, (eventType, parsed) => {
      const data = parsed as Record<string, unknown>;
      if (eventType === "message") {
        result.content = data.content as string;
        result.complete = (data.complete as boolean) ?? false;
        if (data.requirements) {
          result.requirements = data.requirements as InterviewOutput;
        }
        if (data.missing_fields) {
          result.missingFields = data.missing_fields as string[];
        }
        if (data.gathered_fields) {
          result.gatheredFields = data.gathered_fields as Record<string, unknown>;
        }
        if (data.input_hint) {
          result.inputHint = data.input_hint as InputHint;
        }
      }
      if (eventType === "error") {
        throw new Error(data.message as string);
      }
    });
  } finally {
    reader.cancel().catch(() => {});
  }

  return result;
}


export async function submitDocsTask(
  tenantId = "default",
  projectId = "default",
): Promise<DocsTaskResponse> {
  const res = await fetch(`${API}${withTenant("/api/docs/submit", tenantId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_id: projectId }),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(sanitizeApiError(text, res.status));
  }

  return res.json() as Promise<DocsTaskResponse>;
}


export async function getDocsTask(
  taskId: string,
  tenantId = "default",
): Promise<DocsTaskResponse> {
  const res = await fetch(
    `${API}${withTenant(`/api/docs/task/${taskId}`, tenantId)}`,
    { headers: { "Content-Type": "application/json" } },
  );

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(sanitizeApiError(text, res.status));
  }

  return res.json() as Promise<DocsTaskResponse>;
}


export async function regenerateDocsSection(
  projectId: string,
  section: string,
  tenantId = "default",
): Promise<RegenerateDocsSectionResponse> {
  const res = await fetch(`${API}${withTenant("/api/docs/regenerate-section", tenantId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_id: projectId, section }),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(sanitizeApiError(text, res.status));
  }

  return res.json() as Promise<RegenerateDocsSectionResponse>;
}
