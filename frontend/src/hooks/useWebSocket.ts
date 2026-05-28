"use client";

import { useEffect, useRef, useCallback } from "react";
import type { WsMessage } from "@/lib/types";
import { getWsUrl } from "@/lib/api";

export function useWebSocket(onMessage: (msg: WsMessage) => void) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const onMessageRef = useRef(onMessage);

  // Keep callback ref current without re-triggering effect
  onMessageRef.current = onMessage;

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(getWsUrl());
    wsRef.current = ws;

    ws.onopen = () => {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = undefined;
      }
    };

    ws.onmessage = (event) => {
      try {
        const data: WsMessage = JSON.parse(event.data);
        onMessageRef.current(data);
      } catch {
        // ignore parse errors
      }
    };

    ws.onclose = () => {
      reconnectTimerRef.current = setTimeout(connect, 3000);
    };

    ws.onerror = () => {
      // onclose will handle reconnect
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimerRef.current)
        clearTimeout(reconnectTimerRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return wsRef;
}
