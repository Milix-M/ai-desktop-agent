import { webcrypto } from "node:crypto";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import Home from "./page";
import { ApiError, controlTask, createTask, getCurrentTask, getTaskDetail, getTaskHistory, getVms } from "@/lib/api";
import type { TaskDetail, TaskStatus } from "@/lib/types";

vi.mock("@/components/VncViewer", () => ({ default: () => <div /> }));
vi.mock("@/components/VMControls", () => ({ default: () => null }));
vi.mock("@/components/ConnectionPanel", () => ({ default: () => null }));
const ws = vi.hoisted(() => ({ handler: null as null | ((message: import("@/lib/types").WsMessage) => void) }));
vi.mock("@/hooks/useWebSocket", () => ({ useWebSocket: (handler: typeof ws.handler) => { ws.handler = handler; } }));
vi.mock("@/lib/api", () => ({
  ApiError: class extends Error { constructor(public readonly status: number, message: string) { super(message); } },
  getAuthStatus: async () => ({ authenticated: true }),
  getVms: vi.fn(), getCurrentTask: vi.fn(), getTaskHistory: vi.fn(), getTaskDetail: vi.fn(),
  controlTask: vi.fn(), createTask: vi.fn(), deleteTask: vi.fn(), createVm: vi.fn(),
  getVncWsUrl: () => "ws://localhost/vnc/desktop",
}));

const idle: TaskStatus = { session_id: null, vm_id: "desktop", state: "idle", is_running: false,
  action_count: 0, success_count: 0, failure_count: 0, subtasks: [], current_subtask_index: 0 };
const archived: TaskDetail = { id: "archived", vm_id: "desktop", instruction: "保存済みの作業",
  state: "completed", success: true, action_count: 0, success_count: 0, failure_count: 0,
  subtasks: [], actions: [], current_subtask_index: 0, updated_at: 1, created_at: 1, goal: {} };

beforeEach(() => {
  vi.resetAllMocks(); sessionStorage.clear(); vi.stubGlobal("crypto", webcrypto);
  vi.mocked(getVms).mockResolvedValue([{ id: "desktop", name: "作業環境", status: "running", health: "healthy",
    managed: false, kind: "container", vnc_host: "desktop", vnc_port: 5900, ws_port: 0 }]);
  vi.mocked(getCurrentTask).mockResolvedValue(idle);
  vi.mocked(getTaskHistory).mockResolvedValue([archived]);
  vi.mocked(getTaskDetail).mockResolvedValue(archived);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function startPreflight() {
  let finish!: (value: ArrayBuffer) => void;
  const digest = vi.spyOn(crypto.subtle, "digest").mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  render(<Home />);
  const input = await screen.findByRole("textbox", { name: "指示" });
  await waitFor(() => expect(input).toBeEnabled());
  act(() => ws.handler?.({ type: "snapshot", tasks: [] }));
  fireEvent.change(input, { target: { value: "文書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  await waitFor(() => expect(digest).toHaveBeenCalledOnce());
  expect(createTask).not.toHaveBeenCalled();
  return () => finish(new ArrayBuffer(32));
}

test("task preflight cannot clear newer manual-input state when admission is rejected", async () => {
  vi.mocked(createTask).mockRejectedValueOnce(new ApiError(409, "手動入力を待っています"));
  const finish = await startPreflight();
  act(() => ws.handler?.({ type: "state", session_id: "manual-task", session_order: 5, vm_id: "desktop",
    state: "awaiting_user", revision: 3, action_count: 0, subtask_index: 0, subtask_count: 0,
    manual_input: { id: "consent", reason: "接続先を確認して入力してください" } }));
  expect(screen.getByText("接続先を確認して入力してください")).toBeVisible();
  await act(async () => finish());
  await waitFor(() => expect(createTask).toHaveBeenCalledOnce());
  expect(screen.getByText("接続先を確認して入力してください")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "画面を送信して再開" }));
  await waitFor(() => expect(controlTask).toHaveBeenCalledExactlyOnceWith("resume", "desktop", "manual-task", "consent"));
});

test("finishing task preflight preserves history selected after the user pressed run", async () => {
  vi.mocked(createTask).mockResolvedValueOnce({ ...idle, session_id: "new", session_order: 5,
    state: "executing", is_running: true });
  const finish = await startPreflight();
  fireEvent.click(screen.getByTitle("保存済みの作業"));
  await screen.findByText("タスク: 保存済みの作業（完了）");
  await act(async () => finish());
  await waitFor(() => expect(createTask).toHaveBeenCalledOnce());
  expect(screen.getByText("タスク: 保存済みの作業（完了）")).toBeVisible();
  expect(screen.getByTitle("保存済みの作業").closest(".history-item")).toHaveClass("selected");
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  await waitFor(() => expect(controlTask).toHaveBeenCalledExactlyOnceWith("stop", "desktop", "new", undefined));
});
