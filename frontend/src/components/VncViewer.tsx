"use client";

import { useEffect, useRef, useState } from "react";
import { getVncWsUrl } from "@/lib/api";

interface Props {
  onConnectionChange?: (connected: boolean, resolution?: string) => void;
}

export default function VncViewer({ onConnectionChange }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const rfbRef = useRef<unknown>(null);
  const [status, setStatus] = useState("未接続");
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function init() {
      try {
        const { default: RFB } = await import("@novnc/novnc");
        if (cancelled || !containerRef.current) return;

        const rfb = new RFB(containerRef.current, getVncWsUrl(), {
          credentials: { password: "" },
          shared: true,
          wsProtocols: ["binary"],
        });
        rfbRef.current = rfb;
        rfb.viewOnly = true;
        rfb.scaleViewport = true;
        rfb.resizeSession = false;

        (rfb as any).addEventListener("connect", () => {
          if (cancelled) return;
          setStatus("接続中");
          setConnected(true);
          const w = (rfb as any).fbWidth;
          const h = (rfb as any).fbHeight;
          onConnectionChange?.(true, w && h ? `${w}x${h}` : undefined);
        });

        (rfb as any).addEventListener("disconnect", (e: any) => {
          if (cancelled) return;
          setConnected(false);
          onConnectionChange?.(false);
          if (e.detail.clean) {
            setStatus("切断");
          } else {
            setStatus("再接続中...");
            setTimeout(() => {
              if (!cancelled && rfbRef.current) {
                (rfbRef.current as any).connect();
              }
            }, 3000);
          }
        });
      } catch (e) {
        if (!cancelled) {
          setStatus("接続エラー");
          console.error("noVNC:", e);
        }
      }
    }

    init();

    return () => {
      cancelled = true;
      if (rfbRef.current) {
        try {
          (rfbRef.current as any).disconnect();
        } catch {
          // ignore
        }
      }
    };
  }, [onConnectionChange]);

  return (
    <div className="vnc-panel">
      <div ref={containerRef} className="vnc-screen" />
    </div>
  );
}
