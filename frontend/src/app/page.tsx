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
import ConnectionPanel from "@/components/ConnectionPanel";
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

interface VmView {
  state: string;
  subtaskIndex: number;
  subtaskCount: number;
  subtasks: SubtaskInfo[];
  logs: LogEntry[];
}

const EMPTY_VIEW: VmView = {
  state: "idle",
  subtaskIndex: 0,
  subtaskCount: 0,
  subtasks: [],
  logs: [],
};

export default function Home() {
  const [vncConnected, setVncConnected] = useState(false);
  const [vmResolution, setVmResolution] = useState<string | undefined>();
  const [history, setHistory] = useState<TaskHistoryItem[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [vms, setVms] = useState<VmInfo[]>([]);
  const [selectedVmId, setSelectedVmId] = useState<string | null>(null);
  const [creatingVm, setCreatingVm] = useState(false);
  const [vncReset, setVncReset] = useState(0);
  // VM単位の表示状態（ログ・ステータス・進捗はVM間で共有しない）
  const [vmViews, setVmViews] = useState<Record<string, VmView>>({});
  const { width: sidebarWidth, onResizeStart, onResizeMove, onResizeEnd } = useSidebarWidth();
  const mountedRef = useRef(false);
  // WSハンドラから参照する選択中VM（stale closure回避）
  const selectedVmRef = useRef<string | null>(null);
  selectedVmRef.current = selectedVmId;

  const updateView = useCallback(
    (vmId: string | null | undefined, patch: Partial<VmView>) => {
      const key = vmId ?? selectedVmRef.current ?? "__none__";
      setVmViews((prev) => ({ ...prev, [key]: { ...(prev[key] ?? EMPTY_VIEW), ...patch } }));
    },
    []
  );

  const addLog = useCallback(
    (message: string, level: LogEntry["level"], vmId?: string | null) => {
      const entry: LogEntry = {
        id: logIdCounter++,
        time: timeStr(),
        message,
        level,
      };
      const key = vmId ?? selectedVmRef.current ?? "__none__";
      setVmViews((prev) => {
        const view = prev[key] ?? EMPTY_VIEW;
        return { ...prev, [key]: { ...view, logs: [...view.logs, entry] } };
      });
    },
    []
  );

  const view = (selectedVmId && vmViews[selectedVmId]) || EMPTY_VIEW;
  const { state, subtaskIndex, subtaskCount, subtasks, logs } = view;

  const showTaskDetail = useCallback(
    async (taskId: string) => {
      try {
        const detail = await getTaskDetail(taskId);
        const vmId = detail.vm_id ?? selectedVmRef.current;
        setSelectedTaskId(taskId);
        if (vmId) setSelectedVmId(vmId);
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
        updateView(vmId, {
          state: detail.state,
          subtaskCount: detail.subtasks.length,
          subtaskIndex: detail.current_subtask_index,
          subtasks: detail.subtasks.map((s) => ({ id: s.id, description: s.description })),
          logs: entries,
        });
      } catch {
        addLog("タスク詳細の取得に失敗", "error");
      }
    },
    [addLog, updateView]
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
          const vmId = current.vm_id ?? null;
          if (vmId) setSelectedVmId(vmId);
          updateView(vmId, {
            state: current.state,
            subtaskIndex: current.current_subtask_index,
            subtaskCount: current.subtasks.length,
            subtasks: current.subtasks,
          });
          addLog("実行中のタスクに再接続", "state", vmId);
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
      const vmId =
        "vm_id" in data ? (data.vm_id ?? undefined) : undefined;
      switch (data.type) {
        case "state":
          updateView(vmId, {
            state: data.state,
            subtaskIndex: data.subtask_index,
            subtaskCount: data.subtask_count,
            ...(data.subtasks ? { subtasks: data.subtasks } : {}),
          });
          addLog(data.state, "state", vmId);
          break;

        case "action":
          addLog(
            `${data.action_type} ${data.description || ""}`,
            data.success ? "action" : "error",
            vmId
          );
          break;

        case "error":
          addLog(data.message, "error", vmId);
          break;

        case "complete":
          addLog(
            data.success ? "タスク完了" : "タスク失敗",
            data.success ? "complete" : "error",
            vmId
          );
          updateView(vmId, { state: data.success ? "completed" : "failed" });
          refreshHistory();
          break;
      }
    },
    [addLog, refreshHistory, updateView]
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
    async (instruction: string, allowVmRestart: boolean) => {
      const vmId = selectedVmId;
      setSelectedTaskId(null);
      updateView(vmId, {
        state: "idle",
        subtaskIndex: 0,
        subtaskCount: 0,
        subtasks: [],
        logs: [],
      });
      addLog(`${instruction}`, "action", vmId);
      try {
        const result = await createTask(instruction, vmId, allowVmRestart);
        updateView(result.vm_id ?? vmId, { state: result.state });
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        addLog(`投入エラー: ${msg}`, "error", vmId);
      }
    },
    [addLog, selectedVmId, updateView]
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

  const handleCreateVm = useCallback(async (kind: "qemu" | "container") => {
    const label = kind === "container" ? "コンテナ名（空可）" : "VM名（空可）";
    const name = typeof window !== "undefined" ? window.prompt(label) : null;
    if (name === null) return; // キャンセル
    setCreatingVm(true);
    try {
      const vm = await createVm(name || undefined, kind);
      addLog(`${kind === "container" ? "コンテナ" : "VM"}作成開始: ${vm.name}`, "action");
      await refreshVms();
      setSelectedVmId(vm.id);
    } catch (e: unknown) {
      addLog(`${kind === "container" ? "コンテナ" : "VM"}作成失敗: ${e instanceof Error ? e.message : String(e)}`, "error");
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
          onPointerDown={onResizeStart}
          onPointerMove={onResizeMove}
          onPointerUp={onResizeEnd}
          onPointerCancel={onResizeEnd}
        >
          <span className="sidebar-grip" aria-hidden="true">⋮⋮</span>
        </div>

        <div className="sidebar" style={{ width: sidebarWidth, minWidth: sidebarWidth }}>
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

          <ConnectionPanel
            vncConnected={vncConnected}
            vmResolution={vmResolution}
          />

          <CollapsibleSection title="VM管理（デバッグ）">
            <ControlPanel onControl={handleControl} state={state} />
            <div className="vm-subsection vm-status-block">
              <VMControls onLog={(message, level) => addLog(message, level)} />
            </div>
          </CollapsibleSection>

          <TaskHistory
            items={history}
            selectedId={selectedTaskId}
            onSelect={showTaskDetail}
            onDelete={async (taskId) => {
              try {
                const target = history.find((t) => t.id === taskId);
                await deleteTask(taskId);
                setHistory((prev) => prev.filter((t) => t.id !== taskId));
                if (selectedTaskId === taskId) {
                  setSelectedTaskId(null);
                  updateView(target?.vm_id ?? selectedVmId, {
                    state: "idle",
                    subtaskIndex: 0,
                    subtaskCount: 0,
                    subtasks: [],
                    logs: [],
                  });
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
    </div>
  );
}
