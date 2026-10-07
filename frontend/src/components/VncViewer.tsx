"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getVncWsUrl } from "@/lib/api";

interface Props {
  onConnectionChange?: (connected: boolean, resolution?: string) => void;
}

export default function VncViewer({ onConnectionChange }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const rfbRef = useRef<any>(null);
  const cancelledRef = useRef(false);
  const onChangeRef = useRef(onConnectionChange);
  onChangeRef.current = onConnectionChange;
  const [connected, setConnected] = useState(false);

  const connectRfb = useCallback(async () => {
    let RFB;
    try {
      ({ default: RFB } = await import("@novnc/novnc"));
    } catch (e) {
      if (!cancelledRef.current) {
        console.error("noVNC:", e);
      }
      return;
    }
    if (cancelledRef.current || !containerRef.current) return;

    try {
      rfbRef.current?.disconnect();
    } catch {
      // ignore
    }
    rfbRef.current = null;
    setConnected(false);

    const rfb = new RFB(containerRef.current, getVncWsUrl(), {
      credentials: { password: "" },
      shared: true,
      wsProtocols: ["binary"],
    });
    rfbRef.current = rfb;
    rfb.viewOnly = true;
    rfb.scaleViewport = true;
    rfb.resizeSession = false;

    rfb.addEventListener("connect", () => {
      if (cancelledRef.current) return;
      setConnected(true);
      const w = rfb.fbWidth;
      const h = rfb.fbHeight;
      onChangeRef.current?.(true, w && h ? `${w}x${h}` : undefined);
    });

    rfb.addEventListener("disconnect", (e: any) => {
      if (cancelledRef.current) return;
      setConnected(false);
      onChangeRef.current?.(false);
      if (!e.detail.clean) {
        setTimeout(() => {
          if (!cancelledRef.current && rfbRef.current) {
            rfbRef.current.connect();
          }
        }, 3000);
      }
    });
  }, []);

  useEffect(() => {
    cancelledRef.current = false;
    connectRfb();
    return () => {
      cancelledRef.current = true;
      if (rfbRef.current) {
        try {
          rfbRef.current.disconnect();
        } catch {
          // ignore
        }
      }
    };
  }, [connectRfb]);

  return (
    <div className="vnc-panel">
      <div ref={containerRef} className="vnc-screen" />
      {!connected && (
        <button
          className="vnc-reconnect"
          data-testid="vnc-reconnect"
          onClick={() => connectRfb()}
        >
          再接続
        </button>
      )}
    </div>
  );
}
