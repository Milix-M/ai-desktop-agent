"use client";

import { useState, useCallback, useEffect, useRef, type ReactNode } from "react";
import VncViewer from "@/components/VncViewer";
import { isVmReady } from "@/lib/vm-readiness";
import InstructionInput from "@/components/InstructionInput";
import StatusPanel from "@/components/StatusPanel";
import ControlPanel from "@/components/ControlPanel";
import VMControls from "@/components/VMControls";
import VmTabs from "@/components/VmTabs";
import CollapsibleSection from "@/components/CollapsibleSection";
import TaskHistory from "@/components/TaskHistory";
import LogPanel from "@/components/LogPanel";
import ConnectionPanel from "@/components/ConnectionPanel";
import AuthGate from "@/components/AuthGate";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useSidebarWidth } from "@/hooks/useSidebarWidth";
import { ServerGeneration } from "@/lib/server-generation";
import { pendingVmCreations } from "@/lib/vm-creation-retries";
import { clearTaskSubmissions } from "@/lib/task-submission-retries";
import { ApiError, createTask, controlTask, getCurrentTask, getTaskDetail, getTaskHistory, deleteTask, getVms, createVm, deleteVm, stopVm, startVm, getVncWsUrl } from "@/lib/api";
import type { WsMessage, LogEntry, TaskStatus, TaskHistoryItem, TaskOutput, SubtaskInfo, VmInfo, ManualInputRequest, PendingSubmission } from "@/lib/types";

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
  sessionId?: string | null;
  revision?: number;
  sessionOrder?: number;
  retiredSessionOrder?: number;
  workerStopped?: boolean;
  state: string;
  subtaskIndex: number;
  subtaskCount: number;
  subtasks: SubtaskInfo[];
  logs: LogEntry[];
  result?: TaskOutput[];
  manualInput?: ManualInputRequest | null;
}

const EMPTY_VIEW: VmView = {
  state: "idle",
  subtaskIndex: 0,
  subtaskCount: 0,
  subtasks: [],
  logs: [],
};

function isActiveState(state: string): boolean {
  return ["understanding", "planning", "executing", "waiting", "verifying", "recovering",
    "paused", "awaiting_user"].includes(state);
}

function isOlderView(current: VmView, patch: Partial<VmView>): boolean {
  if (patch.sessionId === undefined) return false;
  if (patch.sessionId === current.sessionId && current.workerStopped &&
      patch.state && isActiveState(patch.state)) return true;
  if (patch.sessionId !== current.sessionId) {
    const order = patch.sessionOrder ?? 0;
    const currentOrder = Math.max(current.sessionOrder ?? 0, current.retiredSessionOrder ?? 0);
    if (order || currentOrder) return order <= currentOrder;
  }
  return Boolean(patch.sessionId && patch.sessionId === current.sessionId &&
    patch.revision !== undefined && current.revision !== undefined &&
    patch.revision < current.revision);
}

function snapshotView(task: TaskStatus): Omit<VmView, "logs"> {
  return {
    sessionId: task.session_id,
    revision: task.revision,
    sessionOrder: task.session_order,
    state: !task.is_running && isActiveState(task.state) ? "idle" : task.state,
    subtaskIndex: task.current_subtask_index,
    subtaskCount: task.subtasks.length,
    subtasks: task.subtasks,
    manualInput: task.manual_input ?? null,
    workerStopped: !task.is_running,
  };
}

export default function Home() {
  const draft = useRef("");
  const submission = useRef<PendingSubmission | null>(null);
  return <AuthGate onLogout={() => { draft.current = ""; submission.current = null; clearTaskSubmissions(); }}>
    {(actions) => <DesktopWorkspace sessionActions={actions} draft={draft} submission={submission} />}
  </AuthGate>;
}

function DesktopWorkspace({ sessionActions, draft, submission }: {
  sessionActions: ReactNode;
  draft: { current: string };
  submission: { current: PendingSubmission | null };
}) {
  const [vncConnected, setVncConnected] = useState(false);
  const [statusSynchronized, setStatusSynchronized] = useState(false);
  const [vmResolution, setVmResolution] = useState<string | undefined>();
  const [history, setHistory] = useState<TaskHistoryItem[]>([]);
  const [detailError, setDetailError] = useState<{ taskId: string; message: string } | null>(null);
  const [selection, setSelection] = useState<{ vmId: string | null; taskId: string | null }>(() => ({
    vmId: submission.current?.vmId ?? null, taskId: null,
  }));
  const { vmId: selectedVmId, taskId: selectedTaskId } = selection;
  const [historyView, setHistoryView] = useState<{ taskId: string; vmId: string | null; view: VmView } | null>(null);
  const [vms, setVms] = useState<VmInfo[]>([]);
  const [creatingVm, setCreatingVm] = useState(false);
  const [vncReset, setVncReset] = useState(0);
  // VM単位の表示状態（ログ・ステータス・進捗はVM間で共有しない）
  const [vmViews, setVmViews] = useState<Record<string, VmView>>({});
  const { width: sidebarWidth, onResizeStart, onResizeMove, onResizeEnd } = useSidebarWidth();
  // WSハンドラから参照する選択中VM（stale closure回避）
  const selectedVmRef = useRef<string | null>(null);
  selectedVmRef.current = selectedVmId;
  const selectionVersion = useRef(0);
  const restoreVersion = useRef(0);
  const detailRequestId = useRef<string | null>(null);
  const historyLoadVersion = useRef(0);
  const vmsLoadVersion = useRef(0);
  const navigationVersion = useRef(submission.current ? 1 : 0);
  const initialSnapshotSeen = useRef(false);
  const latestSessionOrder = useRef(0);
  const generation = useRef(new ServerGeneration());
  const deletedTaskIds = useRef(new Set<string>());
  const unconfirmedDeletion = useRef<string | null>(null);
  const mounted = useRef(true);
  const creationController = useRef<AbortController | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; creationController.current?.abort(); };
  }, []);

  const updateView = useCallback(
    (vmId: string | null | undefined, patch: Partial<VmView>) => {
      latestSessionOrder.current = Math.max(latestSessionOrder.current, patch.sessionOrder ?? 0);
      const key = vmId ?? selectedVmRef.current ?? "__none__";
      setVmViews((prev) => {
        if (patch.sessionId && deletedTaskIds.current.has(patch.sessionId)) return prev;
        const current = prev[key] ?? EMPTY_VIEW;
        if (isOlderView(current, patch)) return prev;
        const defined = Object.fromEntries(Object.entries(patch).filter(([, value]) => value !== undefined));
        const view = { ...current, ...defined };
        if (patch.sessionId && patch.sessionId !== current.sessionId &&
            patch.workerStopped === undefined) view.workerStopped = false;
        if (patch.state && ["completed", "failed", "interrupted"].includes(patch.state)) view.workerStopped = true;
        return { ...prev, [key]: view };
      });
    },
    []
  );

  const addLog = useCallback(
    (message: string, level: LogEntry["level"], vmId?: string | null, identity?: Partial<VmView>) => {
      const entry: LogEntry = {
        id: logIdCounter++,
        time: timeStr(),
        message,
        level,
      };
      const key = vmId ?? selectedVmRef.current ?? "__none__";
      setVmViews((prev) => {
        const view = prev[key] ?? EMPTY_VIEW;
        if (identity && isOlderView(view, identity)) return prev;
        return { ...prev, [key]: { ...view, logs: [...view.logs, entry] } };
      });
    },
    []
  );

  const selectedVm = vms.find((v) => v.id === selectedVmId) ??
    (selectedVmId === null ? vms[0] ?? null : null);
  const liveView = vmViews[selectedVmId ?? "__none__"] ?? EMPTY_VIEW;
  const view = selectedTaskId && historyView && (historyView.vmId === null || historyView.vmId === selectedVmId)
    ? historyView.view
    : liveView;
  const { state, subtaskIndex, subtaskCount, subtasks, logs } = view;

  const showTaskDetail = useCallback(
    async (taskId: string, isCurrent: () => boolean = () => true) => {
      if (!mounted.current) return;
      const requestVersion = ++selectionVersion.current;
      const serverVersion = generation.current.version;
      detailRequestId.current = taskId;
      const requestedVmId = selectedVmRef.current;
      setDetailError(null);
      const relevant = () => mounted.current && requestVersion === selectionVersion.current &&
        serverVersion === generation.current.version &&
        isCurrent();
      try {
        const detail = await getTaskDetail(taskId);
        if (!relevant()) return;
        const vmId = detail.vm_id ?? requestedVmId ?? selectedVmRef.current;
        setSelection((prev) => ({ vmId: vmId ?? prev.vmId, taskId }));
        const entries: LogEntry[] = [
          {
            id: logIdCounter++,
            time: epochStr(detail.updated_at),
            message: `タスク: ${detail.instruction || "(指示なし)"}（${
              STATE_LABEL[detail.state] ?? detail.state
            }）`,
            level: "state",
          },
          {
            id: logIdCounter++,
            time: epochStr(detail.updated_at),
            message:
              `使用量: モデル${detail.execution_usage?.requests ?? detail.llm_calls ?? 0}回` +
              `（入力${detail.execution_usage?.input_tokens ?? detail.prompt_tokens ?? 0}` +
              `/出力${detail.execution_usage?.output_tokens ?? detail.completion_tokens ?? 0}トークン）` +
              `・決定${detail.decision_calls ?? 0}件・退避${detail.decision_escalations ?? 0}件` +
              (detail.execution_usage?.usage_complete === false ? "・使用量未確認" : "") +
              (detail.execution_usage?.estimated_cost_usd != null
                ? `・推定$${detail.execution_usage.estimated_cost_usd}` : ""),
            level: "state",
          },
          ...(detail.error_message ? [{
            id: logIdCounter++,
            time: epochStr(detail.updated_at),
            message: detail.error_message,
            level: "error" as const,
          }] : []),
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
        setHistoryView({
          taskId,
          vmId,
          view: {
            state: detail.state,
            subtaskCount: detail.subtasks.length,
            subtaskIndex: detail.current_subtask_index,
            subtasks: detail.subtasks.map((s) => ({ id: s.id, description: s.description })),
            logs: entries,
            result: detail.result,
          },
        });
      } catch (error) {
        if (relevant()) {
          setDetailError({ taskId,
            message: `詳細を確認できません: ${error instanceof Error ? error.message : String(error)}` });
          if (error instanceof ApiError && error.status === 404) {
            detailRequestId.current = null;
            setSelection((prev) => prev.taskId === taskId ? { ...prev, taskId: null } : prev);
            setHistoryView((prev) => prev?.taskId === taskId ? null : prev);
          }
          addLog("タスク詳細の取得に失敗", "error", requestedVmId);
        }
      }
    },
    [addLog]
  );

  const refreshHistory = useCallback(async () => {
    if (!mounted.current) return;
    const requestVersion = ++historyLoadVersion.current;
    const serverVersion = generation.current.version;
    try {
      const items = await getTaskHistory();
      if (!mounted.current || requestVersion !== historyLoadVersion.current || serverVersion !== generation.current.version) return;
      // Keep the retry button when the server deleted the record but its reply was lost.
      const retryId = unconfirmedDeletion.current;
      setHistory(previous => {
        if (!retryId || items.some(item => item.id === retryId)) return items;
        const retryItem = previous.find(item => item.id === retryId);
        return retryItem ? [...items, retryItem] : items;
      });
    } catch {
      // 履歴なしでも継続
    }
  }, []);

  const refreshVms = useCallback(async () => {
    if (!mounted.current) return;
    const requestVersion = ++vmsLoadVersion.current;
    const serverVersion = generation.current.version;
    try {
      const list = await getVms();
      if (!mounted.current || requestVersion !== vmsLoadVersion.current || serverVersion !== generation.current.version) return;
      setVms(list);
      setSelection((prev) => {
        if (prev.vmId && (prev.taskId || list.some((v) => v.id === prev.vmId))) return prev;
        return { ...prev, vmId: list[0]?.id ?? null };
      });
    } catch {
      // VM一覧なしでも継続
    }
  }, []);

  const acceptServer = useCallback((serverId?: string, startedAt?: number) => {
    if (!mounted.current) return "ignored";
    const previous = generation.current.version;
    const result = generation.current.accept(serverId, startedAt);
    if (result === "reset") {
      deletedTaskIds.current.clear();
      unconfirmedDeletion.current = null;
      setStatusSynchronized(false);
      latestSessionOrder.current = 0;
      initialSnapshotSeen.current = false;
      selectionVersion.current++;
      restoreVersion.current++;
      setVmViews({});
      setHistoryView(null);
      setHistory([]);
      setDetailError(null);
    }
    if (generation.current.version !== previous) {
      void refreshHistory();
      void refreshVms();
      if (detailRequestId.current) void showTaskDetail(detailRequestId.current);
    }
    return result;
  }, [refreshHistory, refreshVms, showTaskDetail]);

  // 初回マウント時：現在の状態を復元（リロード対応）
  useEffect(() => {
    const initialSelection = selectionVersion.current;
    const serverVersion = generation.current.version;
    let disposed = false;
    refreshVms();
    const vmTimer = setInterval(refreshVms, 15000);
    (async () => {
      try {
        const current = await getCurrentTask();
        if (disposed) return;
        if (acceptServer(current.server_id, serverVersion) === "ignored") return;
        if (disposed || initialSelection !== selectionVersion.current ||
            restoreVersion.current !== 0 || navigationVersion.current !== 0) return;
        if (current.session_id && !current.is_running) {
          // 実行中でない永続タスク → 詳細を復元
          await showTaskDetail(current.session_id, () => !disposed && restoreVersion.current === 0);
        } else if (current.session_id && current.is_running) {
          detailRequestId.current = null;
          const vmId = current.vm_id ?? null;
          if (vmId) setSelection({ vmId, taskId: null });
          updateView(vmId, {
            sessionId: current.session_id,
            revision: current.revision,
            sessionOrder: current.session_order,
            state: current.state,
            subtaskIndex: current.current_subtask_index,
            subtaskCount: current.subtasks.length,
            subtasks: current.subtasks,
            manualInput: current.manual_input ?? null,
          });
          addLog("実行中のタスクに再接続", "state", vmId);
        }
      } catch {
        if (!disposed && initialSelection === selectionVersion.current &&
            restoreVersion.current === 0 && navigationVersion.current === 0) addLog("状態の復元に失敗", "error");
      }
      if (generation.current.version === serverVersion) refreshHistory();
    })();
    return () => { disposed = true; clearInterval(vmTimer); };
  }, [acceptServer, addLog, refreshHistory, showTaskDetail, refreshVms, updateView]);

  useEffect(() => {
    const pending = pendingVmCreations();
    if (!pending.length) return;
    const controller = new AbortController();
    setCreatingVm(true);
    void Promise.allSettled(pending.map(async (creation) => {
      try {
        const vm = await createVm(creation.name ?? undefined, creation.kind, controller.signal);
        if (controller.signal.aborted) return;
        addLog(`作成結果を確認: ${vm.name}`, "state", vm.id);
        await refreshVms();
      } catch (error) {
        if (!controller.signal.aborted) addLog(`作成結果の確認: ${error instanceof Error ? error.message : String(error)}`, "error");
      }
    })).finally(() => { if (!controller.signal.aborted) setCreatingVm(false); });
    return () => controller.abort();
  }, [addLog, refreshVms]);

  const handleWsMessage = useCallback(
    (data: WsMessage) => {
      const previousVersion = generation.current.version;
      if (["snapshot", "state", "action", "error", "complete"].includes(data.type)) {
        if (acceptServer(data.server_id) === "ignored") return;
        if ("session_id" in data && data.session_id && deletedTaskIds.current.has(data.session_id)) return;
        restoreVersion.current++;
      }
      const vmId =
        "vm_id" in data ? (data.vm_id ?? undefined) : undefined;
      switch (data.type) {
        case "snapshot":
          const tasks = data.tasks.filter(task => !task.session_id || !deletedTaskIds.current.has(task.session_id));
          setStatusSynchronized(true);
          const restoreSelection = !initialSnapshotSeen.current && navigationVersion.current === 0;
          initialSnapshotSeen.current = true;
          setVmViews((previous) => {
            const present = new Map(tasks.map((task) => [task.vm_id, task]));
            const next = { ...previous };
            for (const [id, current] of Object.entries(previous)) {
              const task = present.get(id);
              const order = Math.max(current.sessionOrder ?? 0, current.retiredSessionOrder ?? 0);
              const confirmed = data.through_session_order === undefined
                ? !task : order <= data.through_session_order;
              const removed = !task || (task.session_id !== current.sessionId &&
                (task.session_order ?? 0) < (current.sessionOrder ?? 0));
              if (confirmed && removed) {
                // Show the surviving record while continuing to reject deleted-task events.
                next[id] = task
                  ? { ...EMPTY_VIEW, ...snapshotView(task), retiredSessionOrder: order }
                  : { ...EMPTY_VIEW, logs: current.logs, sessionId: null, sessionOrder: order };
              }
              if (confirmed && task && task.session_id === current.sessionId && !task.is_running &&
                  isActiveState(current.state)) next[id] = {
                ...current, state: "idle", manualInput: null, workerStopped: true,
              };
            }
            return next;
          });
          for (const task of tasks) {
            if (!task.vm_id) continue;
            updateView(task.vm_id, snapshotView(task));
          }
          if (restoreSelection) {
            const latest = [...tasks].filter((task) => task.vm_id && task.session_id)
              .sort((a, b) => (b.session_order ?? 0) - (a.session_order ?? 0))[0];
            if (latest && (latest.session_order ?? 0) >= latestSessionOrder.current) {
              setSelection((prev) => ({ vmId: latest.vm_id!, taskId: latest.is_running ? null : prev.taskId }));
              if (latest.is_running) {
                detailRequestId.current = null;
                setHistoryView(null);
              } else {
                const version = restoreVersion.current;
                void showTaskDetail(latest.session_id!, () => version === restoreVersion.current);
              }
            }
          }
          if (generation.current.version === previousVersion) refreshHistory();
          break;
        case "state":
          updateView(vmId, {
            sessionId: data.session_id,
            revision: data.revision,
            sessionOrder: data.session_order,
            state: data.state,
            manualInput: data.manual_input ?? null,
            subtaskIndex: data.subtask_index,
            subtaskCount: data.subtask_count,
            ...(data.subtasks ? { subtasks: data.subtasks } : {}),
          });
          addLog(data.state, "state", vmId, {
            sessionId: data.session_id, sessionOrder: data.session_order, revision: data.revision,
            state: data.state,
          });
          break;

        case "action":
          addLog(
            `${data.action_type} ${data.description || ""}`,
            data.success ? "action" : "error",
            vmId,
            { sessionId: data.session_id, sessionOrder: data.session_order }
          );
          break;

        case "error":
          addLog(data.message, "error", vmId, { sessionId: data.session_id, sessionOrder: data.session_order });
          break;

        case "complete":
          addLog(
            data.success ? "タスク完了" : "タスク失敗",
            data.success ? "complete" : "error",
            vmId,
            { sessionId: data.session_id, sessionOrder: data.session_order, revision: data.revision }
          );
          updateView(vmId, {
            sessionId: data.session_id,
            revision: data.revision,
            sessionOrder: data.session_order,
            state: data.success ? "completed" : "failed",
            manualInput: null,
          });
          refreshHistory();
          break;
      }
    },
    [acceptServer, addLog, refreshHistory, showTaskDetail, updateView]
  );

  const handleStatusDisconnect = useCallback(() => setStatusSynchronized(false), []);
  useWebSocket(handleWsMessage, handleStatusDisconnect);

  const handleVncChange = useCallback(
    (connected: boolean, resolution?: string) => {
      setVncConnected(connected);
      setVmResolution(resolution);
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
  const isRunning = RUNNING_STATES.includes(liveView.state);
  const submitDisabled = isRunning || liveView.state === "paused" || liveView.state === "awaiting_user" || !isVmReady(selectedVm);

  // Hashing may finish after navigation or a newer task notification.
  const handleSubmitStart = useCallback((instruction: string) => {
    detailRequestId.current = null;
    navigationVersion.current++;
    selectionVersion.current++;
    setSelection((prev) => ({ ...prev, taskId: null }));
    updateView(selectedVmId, { logs: [] });
    addLog(instruction, "action", selectedVmId);
  }, [addLog, selectedVmId, updateView]);

  const handleSubmit = useCallback(
    async (instruction: string, allowVmRestart: boolean, requestId: string, workspace?: string) => {
      const vmId = selectedVmId;
      const serverVersion = generation.current.version;
      try {
        const result = await createTask(instruction, vmId, allowVmRestart, requestId, ...(workspace ? [workspace] : []));
        if (acceptServer(result.server_id, serverVersion) === "ignored") {
          throw new Error("接続先の更新により受付結果を確認できません。同じ内容で再送してください");
        }
        updateView(result.vm_id ?? vmId, {
          state: result.is_running === false && isActiveState(result.state) ? "idle" : result.state,
          sessionId: result.session_id,
          revision: result.revision,
          sessionOrder: result.session_order,
          manualInput: result.manual_input ?? null,
          subtasks: result.subtasks ?? [],
          subtaskCount: result.subtasks?.length ?? 0,
          subtaskIndex: result.current_subtask_index ?? 0,
          workerStopped: result.is_running === false,
        });
        if (result.replayed) addLog("受付済みのタスクを確認しました", "state", result.vm_id ?? vmId);
      } catch (e: unknown) {
        if (!mounted.current) throw e;
        const error = generation.current.version !== serverVersion && e instanceof ApiError
          ? new Error("接続先の更新により受付結果を確認できません。同じ内容で再送してください", { cause: e }) : e;
        const msg = error instanceof Error ? error.message : String(error);
        addLog(`投入エラー: ${msg}`, "error", vmId);
        throw error;
      }
    },
    [acceptServer, addLog, selectedVmId, updateView]
  );

  const handleControl = useCallback(
    async (action: "pause" | "resume" | "stop") => {
      const vmId = selectedVmId;
      const sessionId = liveView.sessionId;
      const serverVersion = generation.current.version;
      if (!vmId || !sessionId) return;
      try {
        await controlTask(action, vmId, sessionId,
          action === "resume" ? liveView.manualInput?.id : undefined);
      } catch (e: unknown) {
        if (!mounted.current || generation.current.version !== serverVersion) return;
        const msg = e instanceof Error ? e.message : String(e);
        addLog(`操作エラー: ${msg}`, "error", vmId);
        if (e instanceof ApiError && e.status === 409) {
          try {
            const current = await getCurrentTask(vmId);
            if (acceptServer(current.server_id, serverVersion) === "ignored") return;
            if (!current.session_id) {
              setVmViews((prev) => {
                const view = prev[vmId];
                if (!view || view.sessionId !== sessionId) return prev;
                return { ...prev, [vmId]: { ...EMPTY_VIEW, sessionId: null,
                  sessionOrder: view.sessionOrder, workerStopped: true, logs: view.logs } };
              });
              return;
            }
            updateView(vmId, {
              sessionId: current.session_id,
              sessionOrder: current.session_order,
              revision: current.revision,
              state: current.is_running === false && isActiveState(current.state) ? "idle" : current.state,
              manualInput: current.manual_input ?? null,
              subtasks: current.subtasks ?? [],
              subtaskCount: current.subtasks?.length ?? 0,
              subtaskIndex: current.current_subtask_index ?? 0,
              workerStopped: current.is_running === false,
            });
          } catch {
            addLog("最新の状態を取得できません", "error", vmId);
          }
        }
      }
    },
    [acceptServer, addLog, selectedVmId, liveView.sessionId, liveView.manualInput?.id, updateView]
  );

  const handleCreateVm = useCallback(async (kind: "qemu" | "container" | "android", name?: string) => {
    if (creationController.current) return;
    const controller = new AbortController();
    creationController.current = controller;
    navigationVersion.current++;
    const requestVersion = ++selectionVersion.current;
    const requestedVmId = selectedVmRef.current;
    const kindLabel = kind === "container" ? "コンテナ" : kind === "android" ? "Android" : "VM";
    setCreatingVm(true);
    try {
      const vm = await createVm(name || undefined, kind, controller.signal);
      if (controller.signal.aborted) return;
      addLog(`${kindLabel}作成開始: ${vm.name}`, "action", vm.id);
      await refreshVms();
      if (!controller.signal.aborted && requestVersion === selectionVersion.current) {
        detailRequestId.current = null;
        setSelection({ vmId: vm.id, taskId: null });
      }
    } catch (e: unknown) {
      if (!controller.signal.aborted) addLog(`${kindLabel}作成失敗: ${e instanceof Error ? e.message : String(e)}`, "error", requestedVmId);
    } finally {
      creationController.current = null;
      if (!controller.signal.aborted) setCreatingVm(false);
    }
  }, [addLog, refreshVms]);

  const handleDeleteVm = useCallback(async (vmId: string) => {
    try {
      await deleteVm(vmId);
      addLog(`削除: ${vmId}`, "state", vmId);
      await refreshVms();
    } catch (e: unknown) {
      addLog(`削除失敗: ${e instanceof Error ? e.message : String(e)}`, "error", vmId);
    }
  }, [addLog, refreshVms]);

  const handleStopVm = useCallback(async (vmId: string) => {
    try {
      await stopVm(vmId);
      addLog(`停止: ${vmId}（実行中タスクは停止）`, "state", vmId);
      await refreshVms();
    } catch (e: unknown) {
      addLog(`停止失敗: ${e instanceof Error ? e.message : String(e)}`, "error", vmId);
    }
  }, [addLog, refreshVms]);

  const handleStartVm = useCallback(async (vmId: string) => {
    try {
      await startVm(vmId);
      addLog(`起動: ${vmId}`, "state", vmId);
      await refreshVms();
    } catch (e: unknown) {
      addLog(`起動失敗: ${e instanceof Error ? e.message : String(e)}`, "error", vmId);
    }
  }, [addLog, refreshVms]);

  const vncUrl = getVncWsUrl(selectedVm?.id);

  return (
    <div className="app-container">
      <VmTabs
        vms={vms}
        selectedId={selectedVm?.id ?? null}
        onSelect={(vmId) => {
          detailRequestId.current = null;
          navigationVersion.current++;
          selectionVersion.current++;
          setSelection({ vmId, taskId: null });
        }}
        onCreate={handleCreateVm}
        onDelete={handleDeleteVm}
        onStop={handleStopVm}
        onStart={handleStartVm}
        creating={creatingVm}
        actions={sessionActions}
      />
      <div className="main-layout">
        {selectedVm?.status === "running" ? (
          <VncViewer key={`${vncUrl}:${vncReset}`} wsUrl={vncUrl} onConnectionChange={handleVncChange}
            allowInput={statusSynchronized && liveView.state === "awaiting_user" && !!liveView.manualInput?.id && !liveView.workerStopped} />
        ) : <div className="vnc-panel" />}

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
          {!vncConnected && selectedVm?.status === "running" && (
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
            onSubmitStart={handleSubmitStart}
            disabled={submitDisabled}
            initialValue={draft.current}
            onDraftChange={(value) => { draft.current = value; }}
            vmId={selectedVmId}
            allowWorkspace={selectedVm?.workspace_tools}
            submission={submission}
          />

          <StatusPanel
            state={state}
            subtaskIndex={subtaskIndex}
            subtaskCount={subtaskCount}
            subtasks={subtasks}
          />

          <ControlPanel onControl={handleControl} state={liveView.state} manualInput={liveView.manualInput}
            manualInputReady={statusSynchronized}
            disabled={!selectedVmId || !liveView.sessionId} />

          <ConnectionPanel
            statusConnected={statusSynchronized}
            vncConnected={vncConnected}
            vmResolution={vmResolution}
          />

          <CollapsibleSection title="VM管理（デバッグ）">
            <div className="vm-subsection vm-status-block">
              <VMControls onLog={(message, level) => addLog(message, level)} />
            </div>
          </CollapsibleSection>

          <TaskHistory
            items={history}
            selectedId={selectedTaskId}
            detailError={detailError}
            onSelect={(taskId) => { navigationVersion.current++; void showTaskDetail(taskId); }}
            onDelete={async (taskId) => {
              const serverVersion = generation.current.version;
              navigationVersion.current++;
              restoreVersion.current++;
              historyLoadVersion.current++;
              if (detailRequestId.current === taskId) selectionVersion.current++;
              unconfirmedDeletion.current = taskId;
              try {
                await deleteTask(taskId);
              } catch (error) {
                if (!mounted.current || generation.current.version !== serverVersion) return;
                if (!(error instanceof ApiError && error.status === 404)) {
                  throw error;
                }
              }
              if (!mounted.current || generation.current.version !== serverVersion) return;
              unconfirmedDeletion.current = null;
              deletedTaskIds.current.add(taskId);
              historyLoadVersion.current++;
              if (detailRequestId.current === taskId) {
                selectionVersion.current++;
                detailRequestId.current = null;
              }
              setHistory((prev) => prev.filter((t) => t.id !== taskId));
              setDetailError((prev) => prev?.taskId === taskId ? null : prev);
              setSelection((prev) => prev.taskId === taskId ? { ...prev, taskId: null } : prev);
              setHistoryView((current) => current?.taskId === taskId ? null : current);
              setVmViews((prev) => Object.fromEntries(Object.entries(prev).map(([id, current]) => [id,
                current.sessionId === taskId ? { ...EMPTY_VIEW, workerStopped: true,
                  sessionOrder: current.sessionOrder,
                  retiredSessionOrder: Math.max(current.sessionOrder ?? 0, current.retiredSessionOrder ?? 0),
                  logs: current.logs } : current,
              ])));
              addLog("履歴を削除", "state");
            }}
          />

          {view.result && view.result.length > 0 && (
            <section className="section task-result">
              <h2>実行結果</h2>
              {view.result.map((output, index) => <p key={index}>{output.summary}</p>)}
            </section>
          )}
          <LogPanel entries={logs} />
        </div>
      </div>
    </div>
  );
}
