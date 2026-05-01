import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { usePollTask } from "@/hooks/usePollTask";

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

interface FakeTask {
  task_id: string;
  status: "queued" | "processing" | "completed" | "failed";
}

function makeOptions(overrides: Partial<Parameters<typeof usePollTask<FakeTask>>[0]> = {}) {
  return {
    fetchFn: vi.fn<(taskId: string) => Promise<FakeTask>>().mockResolvedValue({
      task_id: "t1",
      status: "processing",
    }),
    isComplete: vi.fn((t: FakeTask) => t.status === "completed"),
    isFailed: vi.fn((t: FakeTask) => t.status === "failed"),
    onComplete: vi.fn(),
    onFailed: vi.fn(),
    onProgress: vi.fn(),
    onTimeout: vi.fn(),
    onError: vi.fn(),
    isWsConnected: vi.fn(() => false),
    intervalMs: 1000,
    maxAttempts: 5,
    ...overrides,
  };
}

/* ------------------------------------------------------------------ */
/*  Tests                                                              */
/* ------------------------------------------------------------------ */

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("usePollTask", () => {
  it("calls fetchFn on startPolling and fires onProgress for in-progress tasks", async () => {
    const opts = makeOptions();
    const { result } = renderHook(() => usePollTask<FakeTask>(opts));

    await act(async () => {
      result.current.startPolling("t1");
    });

    expect(opts.fetchFn).toHaveBeenCalledWith("t1");
    expect(opts.onProgress).toHaveBeenCalledWith({
      task_id: "t1",
      status: "processing",
    });
  });

  it("calls onComplete when task is completed", async () => {
    const completedTask: FakeTask = { task_id: "t1", status: "completed" };
    const opts = makeOptions({
      fetchFn: vi.fn().mockResolvedValue(completedTask),
    });
    const { result } = renderHook(() => usePollTask<FakeTask>(opts));

    await act(async () => {
      result.current.startPolling("t1");
    });

    expect(opts.onComplete).toHaveBeenCalledWith(completedTask);
    expect(opts.onProgress).not.toHaveBeenCalled();
  });

  it("calls onFailed when task has failed", async () => {
    const failedTask: FakeTask = { task_id: "t1", status: "failed" };
    const opts = makeOptions({
      fetchFn: vi.fn().mockResolvedValue(failedTask),
    });
    const { result } = renderHook(() => usePollTask<FakeTask>(opts));

    await act(async () => {
      result.current.startPolling("t1");
    });

    expect(opts.onFailed).toHaveBeenCalledWith(failedTask);
  });

  it("schedules next poll after intervalMs when task is in progress", async () => {
    let callCount = 0;
    const opts = makeOptions({
      fetchFn: vi.fn().mockImplementation(async () => {
        callCount++;
        if (callCount >= 3) return { task_id: "t1", status: "completed" };
        return { task_id: "t1", status: "processing" };
      }),
    });

    const { result } = renderHook(() => usePollTask<FakeTask>(opts));

    // First poll
    await act(async () => {
      result.current.startPolling("t1");
    });

    expect(opts.fetchFn).toHaveBeenCalledTimes(1);
    expect(opts.onProgress).toHaveBeenCalledTimes(1);

    // Advance timer to trigger second poll
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });

    expect(opts.fetchFn).toHaveBeenCalledTimes(2);
    expect(opts.onProgress).toHaveBeenCalledTimes(2);

    // Advance timer to trigger third poll (completes)
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });

    expect(opts.fetchFn).toHaveBeenCalledTimes(3);
    expect(opts.onComplete).toHaveBeenCalledTimes(1);
  });

  it("stops polling when WebSocket is connected", async () => {
    const opts = makeOptions({
      isWsConnected: vi.fn(() => true),
    });
    const { result } = renderHook(() => usePollTask<FakeTask>(opts));

    await act(async () => {
      result.current.startPolling("t1");
    });

    // fetchFn should NOT be called because WS is connected
    expect(opts.fetchFn).not.toHaveBeenCalled();
    expect(opts.onProgress).not.toHaveBeenCalled();
  });

  it("calls onTimeout after maxAttempts", async () => {
    const opts = makeOptions({
      maxAttempts: 2,
      fetchFn: vi.fn().mockResolvedValue({ task_id: "t1", status: "processing" }),
    });
    const { result } = renderHook(() => usePollTask<FakeTask>(opts));

    // First poll (attempt 0)
    await act(async () => {
      result.current.startPolling("t1");
    });

    expect(opts.onProgress).toHaveBeenCalledTimes(1);

    // Second poll (attempt 1)
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });

    expect(opts.onProgress).toHaveBeenCalledTimes(2);

    // Third poll (attempt 2 >= maxAttempts 2) -> timeout
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });

    expect(opts.onTimeout).toHaveBeenCalledTimes(1);
    expect(opts.fetchFn).toHaveBeenCalledTimes(2); // only 2 actual fetches
  });

  it("calls onError when fetchFn throws", async () => {
    const opts = makeOptions({
      fetchFn: vi.fn().mockRejectedValue(new Error("network fail")),
    });
    const { result } = renderHook(() => usePollTask<FakeTask>(opts));

    await act(async () => {
      result.current.startPolling("t1");
    });

    expect(opts.onError).toHaveBeenCalledWith("network fail");
  });

  it("stopPolling cancels scheduled polls", async () => {
    const opts = makeOptions();
    const { result } = renderHook(() => usePollTask<FakeTask>(opts));

    await act(async () => {
      result.current.startPolling("t1");
    });

    expect(opts.onProgress).toHaveBeenCalledTimes(1);

    // Stop before next timer fires
    act(() => {
      result.current.stopPolling();
    });

    await act(async () => {
      vi.advanceTimersByTime(5000);
    });

    // Should NOT have polled again
    expect(opts.fetchFn).toHaveBeenCalledTimes(1);
  });

  it("cleans up timer on unmount", async () => {
    const opts = makeOptions();
    const { result, unmount } = renderHook(() => usePollTask<FakeTask>(opts));

    await act(async () => {
      result.current.startPolling("t1");
    });

    unmount();

    // Advancing time should not cause errors or additional calls
    await act(async () => {
      vi.advanceTimersByTime(5000);
    });

    expect(opts.fetchFn).toHaveBeenCalledTimes(1);
  });
});
