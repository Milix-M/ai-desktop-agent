"use client";

import { useEffect, useState } from "react";
import { getVncWsUrl } from "@/lib/api";

interface StatusBarProps {
  vncConnected: boolean;
  agentState: string;
  vmResolution?: string;
}

interface TimeInfo {
  time: string;
  uptime: string;
}

function useClock(startedAt: number): TimeInfo {
  const [info, setInfo] = useState<TimeInfo>({ time: "", uptime: "" });

  useEffect(() => {
    const tick = () => {
      const now = Date.now();
      const elapsed = Math.floor((now - startedAt) / 1000);
      const h = Math.floor(elapsed / 3600);
      const m = Math.floor((elapsed % 3600) / 60);
      const s = elapsed % 60;
      setInfo({
        time: new Date().toLocaleTimeString("ja-JP"),
        uptime:
          h > 0 ? `${h}h${m}m${s}s` : m > 0 ? `${m}m${s}s` : `${s}s`,
      });
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [startedAt]);

  return info;
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

export default function StatusBar({
  vncConnected,
  agentState,
  vmResolution,
}: StatusBarProps) {
  const [startedAt] = useState(() => Date.now());
  const { time, uptime } = useClock(startedAt);
  const backendAlive = useBackendHealth();

  const vmLabel =
    vmResolution === "720x400"
      ? "Provisioning..."
      : vmResolution
        ? `Desktop ${vmResolution}`
        : "—";

  const stateLabel: Record<string, string> = {
    idle: "待機中",
    understanding: "解析中",
    planning: "計画中",
    executing: "実行中",
    waiting: "待機",
    verifying: "検証中",
    recovering: "回復中",
    completed: "完了",
    failed: "失敗",
    paused: "一時停止",
  };

  return (
    <div className="status-bar">
      <div className="sb-item">
        <span className={`sb-dot ${backendAlive ? "green" : "red"}`} />
        <span>Backend {backendAlive ? "OK" : "DOWN"}</span>
      </div>

      <div className="sb-item">
        <span className={`sb-dot ${vncConnected ? "green" : "red"}`} />
        <span>VNC {vncConnected ? "接続中" : "未接続"}</span>
      </div>

      <div className="sb-item">
        <span className="sb-dot blue" />
        <span>VM {vmLabel}</span>
      </div>

      <div className="sb-item">
        <span className="sb-dot yellow" />
        <span>Agent {stateLabel[agentState] || agentState}</span>
      </div>

      <div className="sb-spacer" />

      <div className="sb-item sb-right">
        <span>🕐 {time}</span>
      </div>

      <div className="sb-item sb-right">
        <span>⏱ {uptime}</span>
      </div>
    </div>
  );
}
