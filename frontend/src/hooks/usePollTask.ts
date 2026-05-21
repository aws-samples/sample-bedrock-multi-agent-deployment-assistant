import { useCallback, useEffect, useRef } from "react";

interface PollTaskOptions<T> {
  fetchFn: (taskId: string) => Promise<T>;
  isComplete: (task: T) => boolean;
  isFailed: (task: T) => boolean;
  onComplete: (task: T) => void;
  onFailed: (task: T) => void;
  onProgress: (task: T) => void;
  onTimeout: () => void;
  onError: (error: string) => void;
  isWsConnected: () => boolean;
  intervalMs: number;
  maxAttempts: number;
}

export function usePollTask<T>(options: PollTaskOptions<T>) {
  const optionsRef = useRef(options);
  useEffect(() => {
    optionsRef.current = options;
  });

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollRef = useRef<(taskId: string, attempt?: number) => Promise<void>>(undefined);

  const poll = useCallback(async (taskId: string, attempt = 0) => {
    const opts = optionsRef.current;

    if (opts.isWsConnected()) return;

    if (attempt >= opts.maxAttempts) {
      opts.onTimeout();
      return;
    }

    try {
      const task = await opts.fetchFn(taskId);

      if (opts.isComplete(task)) {
        opts.onComplete(task);
        return;
      }

      if (opts.isFailed(task)) {
        opts.onFailed(task);
        return;
      }

      opts.onProgress(task);

      timerRef.current = setTimeout(() => {
        pollRef.current?.(taskId, attempt + 1);
      }, opts.intervalMs);
    } catch (err) {
      const error = err instanceof Error ? err.message : "Failed to check task status";
      opts.onError(error);
    }
  }, []);

  useEffect(() => {
    pollRef.current = poll;
  });

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const startPolling = useCallback((taskId: string) => {
    pollRef.current?.(taskId, 0);
  }, []);

  const stopPolling = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  return { startPolling, stopPolling };
}
