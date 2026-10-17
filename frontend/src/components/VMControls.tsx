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
  const [watching, setWatching] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const vm = await getVmStatus();
      const health = vm.health ? ` (${vm.health})` : "";
      setStatus(vm.running ? `起動中: ${vm.status}${health}` : `停止中: ${vm.status}`);
      if (vm.running && vm.health === "healthy") {
        setWatching(false);
        setRestarting(false);
      }
      return vm;
    } catch {
      setStatus("取得失敗（backend未接続？）");
      return null;
    }
  }, []);

  useEffect(() => {
    refresh();
    // 作り直し直後は2秒間隔で追跡し、healthyに戻ったら通常間隔へ
    const timer = setInterval(refresh, watching ? 2000 : pollIntervalMs);
    return () => clearInterval(timer);
  }, [refresh, pollIntervalMs, watching]);

  const handleRestart = useCallback(async () => {
    setConfirming(false);
    setRestarting(true);
    setWatching(true);
    onLog?.("VM作り直し開始", "action");
    try {
      const vm = await restartVm();
      onLog?.(`VM再起動指示OK: ${vm.name ?? vm.status}`, "state");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      onLog?.(`VM作り直し失敗: ${msg}`, "error");
      setRestarting(false);
      setWatching(false);
    } finally {
      refresh();
    }
  }, [onLog, refresh]);

  return (
    <>
      <div className="vm-status" data-testid="vm-status">
        {restarting ? "作り直し中..." : status}
      </div>
      {confirming ? (
        <div className="controls">
          <span className="vm-confirm-label">実行中のタスクは停止します</span>
          <button disabled={restarting} onClick={handleRestart}>
            作り直す
          </button>
          <button onClick={() => setConfirming(false)}>
            やめる
          </button>
        </div>
      ) : (
        <div className="controls">
          <button disabled={restarting} onClick={() => setConfirming(true)}>
            VM作り直し
          </button>
        </div>
      )}
    </>
  );
}
