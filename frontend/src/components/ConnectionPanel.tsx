"use client";

import { useEffect, useState } from "react";

interface Props {
  statusConnected: boolean;
  vncConnected: boolean;
  vmResolution?: string;
}

function useBackendHealth(): boolean {
  const [alive, setAlive] = useState(false);
  useEffect(() => {
    let disposed = false;
    let request: AbortController | undefined;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const check = async () => {
      const controller = new AbortController();
      request = controller;
      timeout = setTimeout(() => controller.abort(), 3000);
      try {
        const backendUrl = `${window.location.protocol}//${window.location.hostname}:8081`;
        const resp = await fetch(`${backendUrl}/health`, { signal: controller.signal, cache: "no-store" });
        if (!disposed) setAlive(resp.ok);
      } catch {
        if (!disposed) setAlive(false);
      } finally {
        clearTimeout(timeout);
        if (!disposed) retry = setTimeout(check, 5000);
      }
    };
    void check();
    return () => {
      disposed = true;
      clearTimeout(retry);
      clearTimeout(timeout);
      request?.abort();
    };
  }, []);
  return alive;
}

export default function ConnectionPanel({ statusConnected, vncConnected, vmResolution }: Props) {
  const backendAlive = useBackendHealth();
  const connected = backendAlive && statusConnected;

  const vmLabel = !vncConnected
    ? "未接続"
    : vmResolution
      ? `Desktop ${vmResolution}`
      : "取得中";

  return (
    <div className="section">
      <h2>接続</h2>
      <div className="conn-list">
        <div className="sb-item">
          <span className={`sb-dot ${connected ? "green" : "red"}`} />
          <span>バックエンド {connected ? "接続中" : "未接続"}</span>
        </div>
        <div className="sb-item">
          <span className={`sb-dot ${vncConnected ? "green" : "red"}`} />
          <span>VNC {vncConnected ? "接続中" : "未接続"}</span>
        </div>
        <div className="sb-item">
          <span className={`sb-dot ${vncConnected ? "blue" : "gray"}`} />
          <span>VM {vmLabel}</span>
        </div>
      </div>
    </div>
  );
}
