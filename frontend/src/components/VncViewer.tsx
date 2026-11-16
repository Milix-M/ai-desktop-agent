"use client";

import { useEffect, useRef } from "react";
import type RFB from "@novnc/novnc";
import { getVncWsUrl } from "@/lib/api";

interface Props {
  onConnectionChange?: (connected: boolean, resolution?: string) => void;
  wsUrl?: string;
}

let rfbModule: Promise<typeof import("@novnc/novnc")> | undefined;
function loadRfb() {
  return rfbModule ??= import("@novnc/novnc").catch((error) => {
    rfbModule = undefined;
    throw error;
  });
}

export default function VncViewer({ onConnectionChange, wsUrl }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const onChangeRef = useRef(onConnectionChange);
  onChangeRef.current = onConnectionChange;

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
        connection.viewOnly = true;
        connection.scaleViewport = true;
        connection.resizeSession = false;

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
          rfb = null;
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
      rfb?.disconnect();
      rfb = null;
    };
  }, [wsUrl]);

  return (
    <div className="vnc-panel">
      <div ref={containerRef} className="vnc-screen" />
    </div>
  );
}
