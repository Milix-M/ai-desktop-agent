"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import VncViewer from "@/components/VncViewer";
import InstructionInput from "@/components/InstructionInput";
import StatusPanel from "@/components/StatusPanel";
import ControlPanel from "@/components/ControlPanel";
import VMControls from "@/components/VMControls";
import VmTabs from "@/components/VmTabs";
import CollapsibleSection from "@/components/CollapsibleSection";
import TaskHistory from "@/components/TaskHistory";
import LogPanel from "@/components/LogPanel";
import StatusBar from "@/components/StatusBar";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useSidebarWidth } from "@/hooks/useSidebarWidth";
import { createTask, controlTask, getCurrentTask, getTaskDetail, getTaskHistory, deleteTask, getVms, createVm, deleteVm, getVncWsUrl } from "@/lib/api";
import type { WsMessage, LogEntry, TaskHistoryItem, SubtaskInfo, VmInfo } from "@/lib/types";

let logIdCounter = 0;

function timeStr(): string {
  return new Date().toLocaleTimeString("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function epochStr(epochSec: number): string {
  if (!epochSec) return "--:--";
  return new Date(epochSec * 1000).toLocaleTimeString("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

const STATE_LABEL: Record<string, string> = {
  completed: "完了",
  failed: "失敗",
  interrupted: "中断（サーバー再起動）",
};

export default function Home() {
  const [state, setState] = useState("idle");
  const [subtaskIndex, setSubtaskIndex] = useState(0);
  const [subtaskCount, setSubtaskCount] = useState(0);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [vncConnected, setVncConnected] = useState(false);
  const [vmResolution, setVmResolution] = useState<string | undefined>();
  const [history, setHistory] = useState<TaskHistoryItem[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [subtasks, setSubtasks] = useState<SubtaskInfo[]>([]);
  const [vms, setVms] = useState<VmInfo[]>([]);
  const [selectedVmId, setSelectedVmId] = useState<string | null>(null);
  const [creatingVm, setCreatingVm] = useState(false);
  const [vncReset, setVncReset] = useState(0);
  const { width: sidebarWidth, onResizeStart } = useSidebarWidth();
  const mountedRef = useRef(false);

  const addLog = useCallback(
    (message: string, level: LogEntry["level"]) => {
      const entry: LogEntry = {
        id: logIdCounter++,
        time: timeStr(),
        message,
        level,
      };
      setLogs((prev) => [...prev, entry]);
    },
    []
  );

  const showTaskDetail = useCallback(
    async (taskId: string) => {
      try {
        const detail = await getTaskDetail(taskId);
        setSelectedTaskId(taskId);
        setState(detail.state);
        setSubtaskCount(detail.subtasks.length);
        setSubtaskIndex(detail.current_subtask_index);
        setSubtasks(
          detail.subtasks.map((s) => ({ id: s.id, description: s.description }))
        );
        const entries: LogEntry[] = [
          {
            id: logIdCounter++,
            time: epochStr(detail.updated_at),
            message: `タスク: ${detail.instruction || "(指示なし)"}（${
              STATE_LABEL[detail.state] ?? detail.state
            }）`,
            level: "state",
          },
          ...detail.actions.flatMap((a) => {
            const main = {
              id: logIdCounter++,
              time: epochStr(a.at),
              message: `${a.action_type} ${a.description || ""}${
                a.error_message ? ` [${a.error_message}]` : ""
              }`,
              level: (a.success ? "action" : "error") as LogEntry["level"],
            };
            if (a.reasoning) {
              const short =
                a.reasoning.length > 140
                  ? a.reasoning.slice(0, 140) + "..."
                  : a.reasoning;
              return [
                main,
                {
                  id: logIdCounter++,
                  time: epochStr(a.at),
                  message: `判断: ${short}`,
                  level: "state" as LogEntry["level"],
                },
              ];
            }
            return [main];
          }),
        ];
        setLogs(entries);
      } catch {
        addLog("タスク詳細の取得に失敗", "error");
      }
    },
    [addLog]
  );

  const refreshHistory = useCallback(async () => {
    try {
      setHistory(await getTaskHistory());
    } catch {
      // 履歴なしでも継続
    }
  }, []);

  const refreshVms = useCallback(async () => {
    try {
      const list = await getVms();
      setVms(list);
      setSelectedVmId((prev) => {
        if (prev && list.some((v) => v.id === prev)) return prev;
        return list[0]?.id ?? null;
      });
    } catch {
      // VM一覧なしでも継続
    }
  }, []);

  // 初回マウント時：現在の状態を復元（リロード対応）
  useEffect(() => {
    if (mountedRef.current) return;
    mountedRef.current = true;
    refreshVms();
    const vmTimer = setInterval(refreshVms, 15000);
    (async () => {
      try {
        const current = await getCurrentTask();
        if (current.session_id && !current.is_running) {
          // 実行中でない永続タスク → 詳細を復元
          await showTaskDetail(current.session_id);
        } else if (current.session_id && current.is_running) {
          setState(current.state);
          setSubtaskIndex(current.current_subtask_index);
          setSubtaskCount(current.subtasks.length);
          setSubtasks(current.subtasks);
          addLog("実行中のタスクに再接続", "state");
        }
      } catch {
        addLog("状態の復元に失敗", "error");
      }
      refreshHistory();
    })();
    return () => clearInterval(vmTimer);
  }, [addLog, refreshHistory, showTaskDetail, refreshVms]);

  const handleWsMessage = useCallback(
    (data: WsMessage) => {
      switch (data.type) {
        case "state":
          setState(data.state);
          setSubtaskIndex(data.subtask_index);
          setSubtaskCount(data.subtask_count);
          if (data.subtasks) setSubtasks(data.subtasks);
          addLog(data.state, "state");
          break;

        case "action":
          addLog(
            `${data.action_type} ${data.description || ""}`,
            data.success ? "action" : "error"
          );
          break;

        case "error":
          addLog(data.message, "error");
          break;

        case "complete":
          addLog(
            data.success ? "タスク完了" : "タスク失敗",
            data.success ? "complete" : "error"
          );
          setState(data.success ? "completed" : "failed");
          refreshHistory();
          break;
      }
    },
    [addLog, refreshHistory]
  );

  useWebSocket(handleWsMessage);

  const handleVncChange = useCallback(
    (connected: boolean, resolution?: string) => {
      setVncConnected(connected);
      if (resolution) setVmResolution(resolution);
    },
    []
  );

  const RUNNING_STATES = [
    "understanding",
    "planning",
    "executing",
    "waiting",
    "verifying",
    "recovering",
  ];
  const isRunning = RUNNING_STATES.includes(state);
  const submitDisabled = isRunning || state === "paused";

  const handleSubmit = useCallback(
    async (instruction: string) => {
      setSelectedTaskId(null);
      setSubtasks([]);
      setSubtaskIndex(0);
      setSubtaskCount(0);
      addLog(`${instruction}`, "action");
      try {
        const result = await createTask(instruction, selectedVmId);
        setState(result.state);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        addLog(`投入エラー: ${msg}`, "error");
      }
    },
    [addLog, selectedVmId]
  );

  const handleControl = useCallback(
    async (action: "pause" | "resume" | "stop") => {
      try {
        await controlTask(action);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        addLog(`操作エラー: ${msg}`, "error");
      }
    },
    [addLog]
  );

  const handleCreateVm = useCallback(async () => {
    const name = typeof window !== "undefined" ? window.prompt("VM名（空可）") : null;
    if (name === null) return; // キャンセル
    setCreatingVm(true);
    try {
      const vm = await createVm(name || undefined);
      addLog(`VM作成開始: ${vm.name}`, "action");
      await refreshVms();
      setSelectedVmId(vm.id);
    } catch (e: unknown) {
      addLog(`VM作成失敗: ${e instanceof Error ? e.message : String(e)}`, "error");
    } finally {
      setCreatingVm(false);
    }
  }, [addLog, refreshVms]);

  const handleDeleteVm = useCallback(async (vmId: string) => {
    if (typeof window !== "undefined" && !window.confirm("このVMを削除しますか？上のタスクは停止します。")) {
      return;
    }
    try {
      await deleteVm(vmId);
      addLog("VM削除", "state");
      await refreshVms();
    } catch (e: unknown) {
      addLog(`VM削除失敗: ${e instanceof Error ? e.message : String(e)}`, "error");
    }
  }, [addLog, refreshVms]);

  const selectedVm = vms.find((v) => v.id === selectedVmId) ?? vms[0] ?? null;
  const vncUrl = getVncWsUrl(selectedVm?.ws_port);

  return (
    <div className="app-container">
      <VmTabs
        vms={vms}
        selectedId={selectedVm?.id ?? null}
        onSelect={setSelectedVmId}
        onCreate={handleCreateVm}
        onDelete={handleDeleteVm}
        creating={creatingVm}
      />
      <div className="main-layout">
        <VncViewer key={`${vncUrl}:${vncReset}`} wsUrl={vncUrl} onConnectionChange={handleVncChange} />

        <div
          className="sidebar-resizer"
          data-testid="sidebar-resizer"
          onMouseDown={onResizeStart}
        />

        <div className="sidebar" style={{ width: sidebarWidth, minWidth: sidebarWidth }}>
          <h1>AI Desktop Agent</h1>

          {!vncConnected && (
            <div className="section">
              <h2>VNC接続</h2>
              <div className="controls">
                <button onClick={() => setVncReset((n) => n + 1)}>
                  再接続
                </button>
              </div>
            </div>
          )}

          <InstructionInput
            onSubmit={handleSubmit}
            disabled={submitDisabled}
          />

          <StatusPanel
            state={state}
            subtaskIndex={subtaskIndex}
            subtaskCount={subtaskCount}
            subtasks={subtasks}
          />

          <ControlPanel onControl={handleControl} state={state} />

          <CollapsibleSection title="VM管理（デバッグ）">
            <VMControls onLog={(message, level) => addLog(message, level)} />
          </CollapsibleSection>

          <TaskHistory
            items={history}
            selectedId={selectedTaskId}
            onSelect={showTaskDetail}
            vmNames={Object.fromEntries(vms.map((v) => [v.id, v.name]))}
            onDelete={async (taskId) => {
              try {
                await deleteTask(taskId);
                setHistory((prev) => prev.filter((t) => t.id !== taskId));
                if (selectedTaskId === taskId) {
                  setSelectedTaskId(null);
                  setLogs([]);
                  setState("idle");
                }
                addLog("履歴を削除", "state");
              } catch {
                addLog("履歴削除に失敗", "error");
              }
            }}
          />

          <LogPanel entries={logs} />
        </div>
      </div>

      <StatusBar
        vncConnected={vncConnected}
        agentState={state}
        vmResolution={vmResolution}
      />
    </div>
  );
}
