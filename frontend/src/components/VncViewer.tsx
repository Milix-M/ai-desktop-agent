"use client";

import { useEffect, useRef } from "react";
import type RFB from "@novnc/novnc";
import { getVncWsUrl } from "@/lib/api";
import { trackVncInput } from "@/lib/vnc-input";

interface Props {
  onConnectionChange?: (connected: boolean, resolution?: string) => void;
  wsUrl?: string;
  allowInput?: boolean;
}

let rfbModule: Promise<typeof import("@novnc/novnc")> | undefined;
function loadRfb() {
  return rfbModule ??= import("@novnc/novnc").catch((error) => {
    rfbModule = undefined;
    throw error;
  });
}

export default function VncViewer({ onConnectionChange, wsUrl, allowInput = false }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const connectionRef = useRef<RFB | null>(null);
  const inputRef = useRef<ReturnType<typeof trackVncInput> | null>(null);
  const allowInputRef = useRef(allowInput);
  allowInputRef.current = allowInput;
  const onChangeRef = useRef(onConnectionChange);
  onChangeRef.current = onConnectionChange;

  useEffect(() => {
    if (connectionRef.current) {
      if (!allowInput) inputRef.current?.release();
      connectionRef.current.viewOnly = !allowInput;
    }
  }, [allowInput]);

  useEffect(() => {
    let disposed = false;
    let rfb: RFB | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const target = containerRef.current;
    onChangeRef.current?.(false);

    async function connect() {
      try {
        const { default: RFB } = await loadRfb();
        if (disposed || !target) return;

        // RFB connects in its constructor; disconnected instances cannot reconnect.
        const connection = new RFB(target, wsUrl ?? getVncWsUrl(), {
          credentials: { password: "" },
          shared: true,
          wsProtocols: ["binary"],
        });
        rfb = connection;
        connectionRef.current = connection;
        connection.viewOnly = !allowInputRef.current;
        connection.scaleViewport = true;
        connection.resizeSession = false;
        inputRef.current = trackVncInput(target, connection);

        connection.addEventListener("connect", () => {
          if (disposed || rfb !== connection) return;
          const canvas = target.querySelector("canvas");
          const resolution = canvas?.width && canvas.height
            ? `${canvas.width}x${canvas.height}`
            : undefined;
          onChangeRef.current?.(true, resolution);
        });
        connection.addEventListener("disconnect", () => {
          if (disposed || rfb !== connection) return;
          inputRef.current?.dispose();
          inputRef.current = null;
          rfb = null;
          connectionRef.current = null;
          onChangeRef.current?.(false);
          // A server shutdown may be reported as a clean disconnect too.
          timer = setTimeout(connect, 3000);
        });
      } catch (error) {
        if (disposed) return;
        console.error("noVNC:", error);
        onChangeRef.current?.(false);
        timer = setTimeout(connect, 3000);
      }
    }

    void connect();
    return () => {
      disposed = true;
      clearTimeout(timer);
      inputRef.current?.dispose();
      inputRef.current = null;
      rfb?.disconnect();
      connectionRef.current = null;
      rfb = null;
    };
  }, [wsUrl]);

  return (
    <div className="vnc-panel">
      <div ref={containerRef} className="vnc-screen" />
    </div>
  );
}
