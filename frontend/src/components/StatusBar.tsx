"use client";

import { useEffect, useState } from "react";

interface StatusBarProps {
  vncConnected: boolean;
  agentState: string;
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

export default function StatusBar({
  vncConnected,
  agentState,
  vmResolution,
}: StatusBarProps) {
  const backendAlive = useBackendHealth();

  const vmLabel = !vncConnected
    ? "未接続"
    : vmResolution
      ? `Desktop ${vmResolution}`
      : "取得中";

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
    interrupted: "中断",
    paused: "一時停止",
    no_session: "なし",
  };

  const RUNNING = [
    "understanding",
    "planning",
    "executing",
    "waiting",
    "verifying",
    "recovering",
  ];
  const agentDot = RUNNING.includes(agentState)
    ? "green pulse"
    : agentState === "failed" || agentState === "interrupted"
      ? "red"
      : agentState === "completed"
        ? "blue"
        : agentState === "paused"
          ? "yellow"
          : "gray";

  return (
    <div className="status-bar">
      <div className="sb-item">
        <span className={`sb-dot ${backendAlive ? "green" : "red"}`} />
        <span>バックエンド {backendAlive ? "OK" : "NG"}</span>
      </div>

      <div className="sb-item">
        <span className={`sb-dot ${vncConnected ? "green" : "red"}`} />
        <span>VNC {vncConnected ? "接続中" : "未接続"}</span>
      </div>

      <div className="sb-item">
        <span className={`sb-dot ${vncConnected ? "blue" : "gray"}`} />
        <span>VM {vmLabel}</span>
      </div>

      <div className="sb-item">
        <span className={`sb-dot ${agentDot}`} />
        <span>エージェント {stateLabel[agentState] || agentState}</span>
      </div>
    </div>
  );
}
