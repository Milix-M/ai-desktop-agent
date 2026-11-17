"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getVmStatus, restartVm } from "@/lib/api";
import { isVmReady, vmStatusLabel } from "@/lib/vm-readiness";

interface Props {
  onLog?: (message: string, level: "action" | "error" | "state") => void;
  pollIntervalMs?: number;
  restartTimeoutMs?: number;
}

export default function VMControls({ onLog, pollIntervalMs = 5000, restartTimeoutMs = 600000 }: Props) {
  const [status, setStatus] = useState("取得中...");
  const [restarting, setRestarting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const operation = useRef<{ pending: boolean } | null>(null);
  const mounted = useRef(true);
  const log = useRef(onLog);
  log.current = onLog;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; operation.current = null; };
  }, []);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;

    async function refresh() {
      const observedOperation = operation.current;
      const canConfirm = observedOperation !== null && !observedOperation.pending;
      controller = new AbortController();
      deadline = setTimeout(() => controller?.abort(), 10000);
      try {
        const vm = await getVmStatus(controller.signal);
        if (disposed) return;
        setStatus(vmStatusLabel(vm));
        const current = operation.current;
        // A status fetched before the restart command completed cannot confirm it.
        if (current && current === observedOperation && canConfirm) {
          if (isVmReady(vm)) {
            operation.current = null;
            setRestarting(false);
          } else if (vm.health === "unhealthy" || ["exited", "dead", "not_found", "paused", "removing"].includes(vm.status)) {
            operation.current = null;
            setRestarting(false);
            const message = `再起動に失敗しました（${vmStatusLabel(vm)}）`;
            setStatus(message);
            log.current?.(message, "error");
          }
        }
      } catch {
        if (!disposed) setStatus("状態を取得できません");
      } finally {
        clearTimeout(deadline);
        if (!disposed) timer = setTimeout(refresh, restarting ? 2000 : pollIntervalMs);
      }
    }
    void refresh();
    return () => {
      disposed = true;
      clearTimeout(timer);
      clearTimeout(deadline);
      controller?.abort();
    };
  }, [pollIntervalMs, restarting, refreshVersion]);

  useEffect(() => {
    if (!restarting) return;
    const current = operation.current;
    const timer = setTimeout(() => {
      if (!current || operation.current !== current) return;
      operation.current = null;
      setRestarting(false);
      const message = "再起動の完了を確認できません。状態を確認してから操作してください";
      setStatus(message);
      log.current?.(message, "error");
    }, restartTimeoutMs);
    return () => clearTimeout(timer);
  }, [restarting, restartTimeoutMs]);

  const handleRestart = useCallback(async () => {
    if (operation.current) return;
    setConfirming(false);
    const current = { pending: true };
    operation.current = current;
    setRestarting(true);
    log.current?.("VM再起動開始", "action");
    try {
      const vm = await restartVm();
      if (!mounted.current || operation.current !== current) return;
      current.pending = false;
      log.current?.(`VM再起動指示OK: ${vm.name ?? vm.status}`, "state");
      setRefreshVersion(value => value + 1);
    } catch (error: unknown) {
      if (!mounted.current || operation.current !== current) return;
      operation.current = null;
      setRestarting(false);
      const message = `VM再起動失敗: ${error instanceof Error ? error.message : String(error)}`;
      setStatus(message);
      log.current?.(message, "error");
    }
  }, []);

  return (
    <>
      <div className="vm-status" data-testid="vm-status">
        {restarting ? "再起動中..." : status}
      </div>
      {confirming ? (
        <div className="controls">
          <span className="vm-confirm-label">実行中のタスクは停止します</span>
          <button disabled={restarting} onClick={handleRestart}>
            再起動する
          </button>
          <button onClick={() => setConfirming(false)}>
            やめる
          </button>
        </div>
      ) : (
        <div className="controls">
          <button disabled={restarting} onClick={() => setConfirming(true)}>
            VM再起動
          </button>
        </div>
      )}
    </>
  );
}
