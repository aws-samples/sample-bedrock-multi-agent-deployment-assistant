"use client";

import { useEffect, useRef, useCallback, useState } from "react";
import { getStoredToken } from "@/lib/auth";

const WS_URL = process.env.NEXT_PUBLIC_WEBSOCKET_URL;

const MAX_RECONNECT_DELAY_MS = 30_000;
const INITIAL_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_ATTEMPTS = 20;

interface UseWebSocketOptions {
  projectId: string;
  tenantId?: string;
  onDesignComplete?: (result: unknown) => void;
  onDesignFailed?: (error: string) => void;
  onIaCStatus?: (status: string) => void;
  onIaCComplete?: (result: unknown) => void;
  onIaCFailed?: (error: string) => void;
  onDocsStatus?: (status: string) => void;
  onDocsSection?: (section: string, content: string) => void;
  onDocsComplete?: (result: unknown) => void;
  onDocsFailed?: (error: string) => void;
}

export function useWebSocket({
  projectId,
  tenantId = "default",
  onDesignComplete,
  onDesignFailed,
  onIaCStatus,
  onIaCComplete,
  onIaCFailed,
  onDocsStatus,
  onDocsSection,
  onDocsComplete,
  onDocsFailed,
}: UseWebSocketOptions) {
  const [connected, setConnected] = useState(false);
  const [reconnectExhausted, setReconnectExhausted] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const onDesignCompleteRef = useRef(onDesignComplete);
  const onDesignFailedRef = useRef(onDesignFailed);
  const onIaCStatusRef = useRef(onIaCStatus);
  const onIaCCompleteRef = useRef(onIaCComplete);
  const onIaCFailedRef = useRef(onIaCFailed);
  const onDocsStatusRef = useRef(onDocsStatus);
  const onDocsSectionRef = useRef(onDocsSection);
  const onDocsCompleteRef = useRef(onDocsComplete);
  const onDocsFailedRef = useRef(onDocsFailed);

  // Sync callback refs outside render phase
  useEffect(() => {
    onDesignCompleteRef.current = onDesignComplete;
    onDesignFailedRef.current = onDesignFailed;
    onIaCStatusRef.current = onIaCStatus;
    onIaCCompleteRef.current = onIaCComplete;
    onIaCFailedRef.current = onIaCFailed;
    onDocsStatusRef.current = onDocsStatus;
    onDocsSectionRef.current = onDocsSection;
    onDocsCompleteRef.current = onDocsComplete;
    onDocsFailedRef.current = onDocsFailed;
  });

  const cleanup = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (wsRef.current) {
      // Null out handlers BEFORE close to prevent the async onclose
      // from scheduling a stale reconnect timer.
      wsRef.current.onopen = null;
      wsRef.current.onmessage = null;
      wsRef.current.onclose = null;
      wsRef.current.onerror = null;
      wsRef.current.close();
      wsRef.current = null;
    }
    setConnected(false);
  }, []);

  const connectRef = useRef<() => void>(undefined);
  const connect = useCallback(() => {
    if (!WS_URL) return;

    cleanup();

    // WebSocket API doesn't support custom headers, so the token is passed as a
    // query param. In production the API Gateway authorizer validates and discards
    // it on $connect — the token is not logged by AWS infrastructure. For proxies
    // that log URLs, consider a short-lived exchange ticket pattern.
    const token = getStoredToken();
    const url = token ? `${WS_URL}?token=${encodeURIComponent(token)}` : WS_URL;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      reconnectAttemptRef.current = 0;

      ws.send(
        JSON.stringify({
          action: "subscribe",
          project_id: projectId,
          tenant_id: tenantId,
        }),
      );
    };

    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data as string) as Record<string, unknown>;
        const type = message.type as string | undefined;

        // Design task messages
        if (type === "design_complete" && onDesignCompleteRef.current) {
          onDesignCompleteRef.current(message.result);
        } else if (type === "design_failed" && onDesignFailedRef.current) {
          onDesignFailedRef.current((message.error as string) ?? "Design task failed");
        }

        // IaC task messages
        if (type === "iac_status" && onIaCStatusRef.current) {
          onIaCStatusRef.current(message.status as string);
        } else if (type === "iac_complete" && onIaCCompleteRef.current) {
          onIaCCompleteRef.current(message.result);
        } else if (type === "iac_failed" && onIaCFailedRef.current) {
          onIaCFailedRef.current((message.error as string) ?? "IaC task failed");
        }

        // Docs task messages
        if (type === "docs_status" && onDocsStatusRef.current) {
          onDocsStatusRef.current(message.status as string);
        } else if (type === "docs_section" && onDocsSectionRef.current) {
          onDocsSectionRef.current(
            message.section as string,
            message.content as string,
          );
        } else if (type === "docs_complete" && onDocsCompleteRef.current) {
          onDocsCompleteRef.current(message.result);
        } else if (type === "docs_failed" && onDocsFailedRef.current) {
          onDocsFailedRef.current((message.error as string) ?? "Docs task failed");
        }
      } catch {
        // Ignore malformed messages
      }
    };

    ws.onclose = () => {
      setConnected(false);
      wsRef.current = null;

      const attempt = reconnectAttemptRef.current;
      if (attempt >= MAX_RECONNECT_ATTEMPTS) {
        setReconnectExhausted(true);
        return;
      }

      const delay = Math.min(
        INITIAL_RECONNECT_DELAY_MS * Math.pow(2, attempt),
        MAX_RECONNECT_DELAY_MS,
      );
      reconnectAttemptRef.current = attempt + 1;

      reconnectTimerRef.current = setTimeout(() => {
        connectRef.current?.();
      }, delay);
    };

    ws.onerror = () => {
      // onclose will fire after onerror, triggering reconnect
    };
  }, [projectId, tenantId, cleanup]);
  useEffect(() => { connectRef.current = connect; });

  useEffect(() => {
    if (!WS_URL) return;

    connect(); // eslint-disable-line react-hooks/set-state-in-effect -- setConnected is inside ws.onopen callback, not synchronous

    return () => {
      cleanup();
    };
  }, [connect, cleanup]);

  return { connected, reconnectExhausted };
}
