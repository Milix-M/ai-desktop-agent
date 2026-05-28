"use client";

import { useEffect, useRef, useState } from "react";
import { getVncWsUrl } from "@/lib/api";

export default function VncViewer() {
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

        const rfb = new RFB(
          containerRef.current,
          getVncWsUrl(),
          {
            credentials: { password: "" },
            shared: true,
            wsProtocols: ["binary"],
          }
        );
        rfbRef.current = rfb;
        rfb.viewOnly = true;
        rfb.scaleViewport = true;
        rfb.resizeSession = false;

        (rfb as any).addEventListener("connect", () => {
          if (cancelled) return;
          setStatus("VNC接続中");
          setConnected(true);
        });

        (rfb as any).addEventListener("disconnect", (e: any) => {
          if (cancelled) return;
          setConnected(false);
          if (e.detail.clean) {
            setStatus("VNC切断 (正常)");
          } else {
            setStatus("VNC切断 (再接続中...)");
            setTimeout(() => {
              if (!cancelled && rfbRef.current) {
                (rfbRef.current as any).connect();
              }
            }, 3000);
          }
        });
      } catch (e) {
        if (!cancelled) {
          setStatus("noVNC読み込み失敗");
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
  }, []);

  return (
    <div className="vnc-panel">
      <div ref={containerRef} className="vnc-screen">
        {!connected && (
          <span className="vnc-placeholder">
            {status.includes("接続中") ? "VMに接続中..." : status}
          </span>
        )}
      </div>
      <div className={`vnc-status ${connected ? "connected" : ""}`}>
        {connected ? "✅ " : "⏳ "}
        {status}
      </div>
    </div>
  );
}
