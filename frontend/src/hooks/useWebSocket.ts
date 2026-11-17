"use client";

import { useEffect, useRef } from "react";
import type { WsMessage } from "@/lib/types";
import { getWsUrl } from "@/lib/api";

export function useWebSocket(onMessage: (msg: WsMessage) => void) {
  const wsRef = useRef<WebSocket | null>(null);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  useEffect(() => {
    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let heartbeat: ReturnType<typeof setInterval> | undefined;
    let pongTimer: ReturnType<typeof setTimeout> | undefined;

    function connect() {
      if (disposed) return;
      const ws = new WebSocket(getWsUrl());
      wsRef.current = ws;

      ws.onopen = () => {
        if (disposed || wsRef.current !== ws) return;
        heartbeat = setInterval(() => {
          if (ws.readyState !== WebSocket.OPEN) return;
          ws.send("ping");
          pongTimer = setTimeout(() => ws.close(), 10000);
        }, 20000);
      };
      ws.onmessage = (event) => {
        if (disposed || wsRef.current !== ws) return;
        let data: WsMessage;
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        if (data.type === "pong") clearTimeout(pongTimer);
        onMessageRef.current(data);
      };
      ws.onclose = () => {
        if (disposed || wsRef.current !== ws) return;
        clearInterval(heartbeat);
        clearTimeout(pongTimer);
        retryTimer = setTimeout(connect, 3000);
      };
    }

    connect();
    return () => {
      disposed = true;
      clearTimeout(retryTimer);
      clearInterval(heartbeat);
      clearTimeout(pongTimer);
      const ws = wsRef.current;
      wsRef.current = null;
      ws?.close();
    };
  }, []);

  return wsRef;
}
