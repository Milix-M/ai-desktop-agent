"use client";

import { useEffect, useRef } from "react";
import type { WsMessage } from "@/lib/types";
import { confirmAuthentication, getWsUrl } from "@/lib/api";
import { parseWsMessage } from "@/lib/task-protocol";

export function useWebSocket(onMessage: (msg: WsMessage) => void, onDisconnect?: () => void) {
  const wsRef = useRef<WebSocket | null>(null);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;
  const onDisconnectRef = useRef(onDisconnect);
  onDisconnectRef.current = onDisconnect;

  useEffect(() => {
    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let heartbeat: ReturnType<typeof setInterval> | undefined;
    let pongTimer: ReturnType<typeof setTimeout> | undefined;
    let ownedSocket: WebSocket;

    function connect() {
      if (disposed) return;
      const ws = new WebSocket(getWsUrl());
      ownedSocket = ws;
      wsRef.current = ws;
      let closed = false;

      ws.onopen = () => {
        if (disposed || wsRef.current !== ws || closed) return;
        heartbeat = setInterval(() => {
          if (ws.readyState !== WebSocket.OPEN) return;
          ws.send("ping");
          pongTimer = setTimeout(() => {
            if (disposed || wsRef.current !== ws || closed) return;
            disconnect();
            ws.close();
          }, 10000);
        }, 20000);
      };
      ws.onmessage = (event) => {
        if (disposed || wsRef.current !== ws || closed) return;
        let data: WsMessage;
        try {
          data = parseWsMessage(event.data);
        } catch {
          disconnect();
          // Browser scripts may use 1000 or application codes in 3000–4999.
          ws.close(4002, "Invalid task-state message");
          return;
        }
        if (data.type === "pong") clearTimeout(pongTimer);
        onMessageRef.current(data);
      };
      function disconnect() {
        if (disposed || wsRef.current !== ws || closed) return;
        closed = true;
        clearInterval(heartbeat);
        clearTimeout(pongTimer);
        // The subscriber may unmount; cleanup must already own this retry.
        retryTimer = setTimeout(connect, 3000);
        onDisconnectRef.current?.();
        // A rejected or expired session is checked by HTTP before reconnecting.
        void confirmAuthentication(() => !disposed && wsRef.current === ws);
      }
      ws.onclose = disconnect;
    }

    connect();
    return () => {
      disposed = true;
      clearTimeout(retryTimer);
      clearInterval(heartbeat);
      clearTimeout(pongTimer);
      // The returned ref is writable; it does not transfer socket ownership.
      const ws = ownedSocket;
      if (wsRef.current === ws) wsRef.current = null;
      ws.close();
    };
  }, []);

  return wsRef;
}
