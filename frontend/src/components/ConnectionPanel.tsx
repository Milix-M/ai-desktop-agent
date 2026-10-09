"use client";

import { useEffect, useState } from "react";

interface Props {
  vncConnected: boolean;
  vmResolution?: string;
}

function useBackendHealth(): boolean {
  const [alive, setAlive] = useState(false);
  useEffect(() => {
    const check = async () => {
      try {
        const BACKEND_URL =
          typeof window !== "undefined"
            ? `${window.location.protocol}//${window.location.hostname}:8081`
            : "http://localhost:8081";
        const resp = await fetch(`${BACKEND_URL}/health`);
        setAlive(resp.ok);
      } catch {
        setAlive(false);
      }
    };
    check();
    const id = setInterval(check, 5000);
    return () => clearInterval(id);
  }, []);
  return alive;
}

export default function ConnectionPanel({ vncConnected, vmResolution }: Props) {
  const backendAlive = useBackendHealth();

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
          <span className={`sb-dot ${backendAlive ? "green" : "red"}`} />
          <span>バックエンド {backendAlive ? "接続中" : "未接続"}</span>
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
