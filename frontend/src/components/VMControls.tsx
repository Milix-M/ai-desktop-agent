"use client";

import { useCallback, useEffect, useState } from "react";
import { getVmStatus, restartVm } from "@/lib/api";

interface Props {
  onLog?: (message: string, level: "action" | "error" | "state") => void;
  pollIntervalMs?: number;
}

export default function VMControls({ onLog, pollIntervalMs = 5000 }: Props) {
  const [status, setStatus] = useState("取得中...");
  const [restarting, setRestarting] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const vm = await getVmStatus();
      const health = vm.health ? ` (${vm.health})` : "";
      setStatus(vm.running ? `起動中: ${vm.status}${health}` : `停止中: ${vm.status}`);
    } catch {
      setStatus("取得失敗（backend未接続？）");
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, pollIntervalMs);
    return () => clearInterval(timer);
  }, [refresh, pollIntervalMs]);

  const handleRestart = useCallback(async () => {
    if (typeof window !== "undefined" && !window.confirm("VMを作り直しますか？実行中のタスクは停止します。")) {
      return;
    }
    setRestarting(true);
    onLog?.("VM作り直し開始", "action");
    try {
      const vm = await restartVm();
      onLog?.(`VM再起動指示OK: ${vm.name ?? vm.status}`, "state");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      onLog?.(`VM作り直し失敗: ${msg}`, "error");
    } finally {
      setRestarting(false);
      refresh();
    }
  }, [onLog, refresh]);

  return (
    <div className="section">
      <h2>VM管理（デバッグ）</h2>
      <div className="vm-status" data-testid="vm-status">
        {restarting ? "作り直し中..." : status}
      </div>
      <div className="controls">
        <button disabled={restarting} onClick={handleRestart}>
          VM作り直し
        </button>
      </div>
    </div>
  );
}
