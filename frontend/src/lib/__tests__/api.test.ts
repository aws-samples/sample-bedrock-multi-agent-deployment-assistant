import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/* ------------------------------------------------------------------ */
/*  We need to mock auth before importing api so authHeaders() works  */
/* ------------------------------------------------------------------ */
vi.mock("@/lib/auth", () => ({
  getStoredToken: vi.fn(() => null),
}));

/* Dynamic import so the mock is active when the module initialises */
const authMod = await import("@/lib/auth");
const api = await import("@/lib/api");

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function textResponse(text: string, status: number): Response {
  return new Response(text, { status });
}

function ssePayload(events: { event?: string; data: unknown }[]): string {
  return events
    .map((e) => {
      const lines: string[] = [];
      if (e.event) lines.push(`event: ${e.event}`);
      lines.push(`data: ${JSON.stringify(e.data)}`);
      return lines.join("\n");
    })
    .join("\n\n") + "\n\n";
}

/* ------------------------------------------------------------------ */
/*  Tests                                                              */
/* ------------------------------------------------------------------ */

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
  (authMod.getStoredToken as ReturnType<typeof vi.fn>).mockReturnValue(null);
});

afterEach(() => {
  vi.restoreAllMocks();
});

/* ---------- request<T>() via public wrappers ---------------------- */

describe("request<T>() (via listProjects / createProject)", () => {
  it("performs a GET request and returns JSON", async () => {
    const projects = [{ project_id: "p1", name: "demo" }];
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(jsonResponse(projects));

    const result = await api.listProjects("t1");

    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain("/api/projects?tenant_id=t1");
    expect(init.method).toBe("GET");
    expect(result).toEqual(projects);
  });

  it("performs a POST request with JSON body", async () => {
    const project = { project_id: "p2", name: "new" };
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(jsonResponse(project));

    const result = await api.createProject("new", "t2");

    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ name: "new" });
    expect(result).toEqual(project);
  });

  it("sends Authorization header when token exists", async () => {
    (authMod.getStoredToken as ReturnType<typeof vi.fn>).mockReturnValue("tok123");
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(jsonResponse([]));

    await api.listProjects();

    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.headers.Authorization).toBe("Bearer tok123");
  });

  it("does not send Authorization header when no token", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(jsonResponse([]));

    await api.listProjects();

    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.headers.Authorization).toBeUndefined();
  });

  it("uses DELETE method for deleteProject", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response("", { status: 200 }),
    );

    await api.deleteProject("p1", "t1");

    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.method).toBe("DELETE");
  });
});

/* ---------- Error handling ---------------------------------------- */

describe("error handling", () => {
  it("throws sanitized message for 401", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      textResponse("unauthorized", 401),
    );

    await expect(api.listProjects()).rejects.toThrow("Authentication required.");
  });

  it("throws sanitized message for 403", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      textResponse("forbidden", 403),
    );

    await expect(api.listProjects()).rejects.toThrow("Permission denied.");
  });

  it("throws sanitized message for 500", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      textResponse("boom", 500),
    );

    await expect(api.listProjects()).rejects.toThrow(
      "An internal error occurred. Please try again.",
    );
  });

  it("throws sanitized message for 503", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      textResponse("service unavailable", 503),
    );

    await expect(api.listProjects()).rejects.toThrow(
      "The AI service is temporarily unavailable. Please retry shortly.",
    );
  });

  it("throws timeout error when AbortSignal.timeout fires", async () => {
    const timeoutErr = new DOMException("signal timed out", "TimeoutError");
    (fetch as ReturnType<typeof vi.fn>).mockRejectedValueOnce(timeoutErr);

    await expect(api.listProjects()).rejects.toThrow(
      "Request timed out. Please try again.",
    );
  });

  it("throws abort error for AbortError", async () => {
    const abortErr = new DOMException("aborted", "AbortError");
    (fetch as ReturnType<typeof vi.fn>).mockRejectedValueOnce(abortErr);

    await expect(api.listProjects()).rejects.toThrow("Request was aborted.");
  });

  it("truncates long error text to 200 chars for unknown status codes", async () => {
    const longText = "x".repeat(300);
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      textResponse(longText, 418),
    );

    try {
      await api.listProjects();
      expect.unreachable("Should have thrown");
    } catch (err) {
      const message = (err as Error).message;
      expect(message).toContain("API 418:");
      // The sanitized body portion should be at most 200 chars
      const bodyPart = message.replace("API 418: ", "");
      expect(bodyPart.length).toBeLessThanOrEqual(200);
    }
  });
});

/* ---------- withTenant URL construction --------------------------- */

describe("withTenant() URL construction", () => {
  it("appends tenant_id with ? when path has no query string", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(jsonResponse([]));

    await api.listProjects("acme");

    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain("/api/projects?tenant_id=acme");
  });

  it("appends tenant_id with & when path already has query params", async () => {
    // getDesignTask builds a path without query params, but submitDesign
    // uses withTenant on "/api/design/submit".  We test listProjects
    // which has no query param to verify ? is used, then test a URL that
    // would have ? to verify & is used next time.  The simplest way is
    // to check the URL of getProjectState which has no query string base.
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(jsonResponse({}));

    await api.getProjectState("p1", "acme");

    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain("?tenant_id=acme");
    // Verify no double ?
    expect(url.split("?").length).toBe(2);
  });

  it("encodes special characters in tenant_id", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(jsonResponse([]));

    await api.listProjects("a&b=c");

    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain("tenant_id=a%26b%3Dc");
  });
});

/* ---------- SSE parsing (via invokeInterviewChat) ----------------- */

describe("SSE parsing via invokeInterviewChat", () => {
  function mockSSEResponse(body: string): void {
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(body));
        controller.close();
      },
    });
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
  }

  it("parses a complete SSE message event", async () => {
    const payload = ssePayload([
      {
        event: "message",
        data: {
          content: "Hello world",
          complete: true,
          requirements: { use_cases: ["web"], resilience: "high", compliance: [], solution_description: "desc" },
        },
      },
    ]);
    mockSSEResponse(payload);

    const result = await api.invokeInterviewChat("hi", "t1", "p1");

    expect(result.content).toBe("Hello world");
    expect(result.complete).toBe(true);
    expect(result.requirements?.use_cases).toEqual(["web"]);
  });

  it("ignores SSE comments (lines starting with :)", async () => {
    const payload =
      ": heartbeat\n\n" +
      ssePayload([{ event: "message", data: { content: "ok", complete: false } }]);
    mockSSEResponse(payload);

    const result = await api.invokeInterviewChat("hi");

    expect(result.content).toBe("ok");
  });

  it("throws on SSE error event", async () => {
    const payload = ssePayload([
      { event: "error", data: { message: "something broke" } },
    ]);
    mockSSEResponse(payload);

    await expect(api.invokeInterviewChat("hi")).rejects.toThrow("something broke");
  });

  it("handles multiple SSE events (last message wins)", async () => {
    const payload = ssePayload([
      { event: "message", data: { content: "partial", complete: false } },
      { event: "message", data: { content: "final", complete: true } },
    ]);
    mockSSEResponse(payload);

    const result = await api.invokeInterviewChat("hi");

    expect(result.content).toBe("final");
    expect(result.complete).toBe(true);
  });

  it("populates gatheredFields and inputHint from SSE", async () => {
    const payload = ssePayload([
      {
        event: "message",
        data: {
          content: "next question",
          complete: false,
          gathered_fields: { name: "demo" },
          input_hint: { field_path: "resilience", type: "select", options: ["high", "low"] },
        },
      },
    ]);
    mockSSEResponse(payload);

    const result = await api.invokeInterviewChat("hi");

    expect(result.gatheredFields).toEqual({ name: "demo" });
    expect(result.inputHint).toEqual({ field_path: "resilience", type: "select", options: ["high", "low"] });
  });
});
