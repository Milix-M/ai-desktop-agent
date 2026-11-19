import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import Home from "./page";
import { createTask, createVm, getCurrentTask, getTaskDetail, getTaskHistory, getVms, getVmStatus, restartVm } from "@/lib/api";
import { vmCreationRetry } from "@/lib/vm-creation-retries";
import type { TaskDetail } from "@/lib/types";

const connections: MockRFB[] = [];
class MockRFB extends EventTarget {
  viewOnly = true;
  scaleViewport = false;
  resizeSession = true;
  disconnect = vi.fn();
  constructor(target: HTMLElement) {
    super();
    const canvas = document.createElement("canvas");
    canvas.width = 1280; canvas.height = 800;
    target.replaceChildren(canvas);
    connections.push(this);
  }
}
vi.mock("@novnc/novnc", () => ({ default: MockRFB }));
vi.mock("@/hooks/useWebSocket", () => ({ useWebSocket: () => undefined }));
vi.mock("@/lib/api", () => ({
  ApiError: class extends Error {}, getAuthStatus: async () => ({ authenticated: true }),
  getVms: vi.fn(), getCurrentTask: vi.fn(), getTaskHistory: vi.fn(), getTaskDetail: vi.fn(),
  createVm: vi.fn(), createTask: vi.fn(), controlTask: vi.fn(), deleteTask: vi.fn(),
  deleteVm: vi.fn(), startVm: vi.fn(), stopVm: vi.fn(), getVmStatus: vi.fn(), restartVm: vi.fn(),
  getVncWsUrl: () => "ws://localhost/vnc/desktop",
}));
const archived: TaskDetail = { id: "saved", instruction: "保存済みの作業", vm_id: "desktop",
  state: "completed", success: true, action_count: 0, success_count: 0, failure_count: 0,
  subtasks: [], actions: [], current_subtask_index: 0, updated_at: 1, created_at: 1, goal: {} };

beforeEach(() => {
  vi.resetAllMocks(); sessionStorage.clear(); connections.length = 0;
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true } as Response);
  vi.mocked(getVms).mockResolvedValue([{ id: "desktop", name: "作業環境", status: "running", health: "healthy",
    managed: false, kind: "container", vnc_host: "desktop", vnc_port: 5900, ws_port: 0 }]);
  vi.mocked(getCurrentTask).mockResolvedValue({ session_id: null, vm_id: "desktop", state: "idle",
    is_running: false, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0 });
  vi.mocked(getTaskHistory).mockResolvedValue([]);
  vi.mocked(getVmStatus).mockResolvedValue({ running: true, status: "running", health: "healthy", name: "desktop" });
  vi.mocked(restartVm).mockResolvedValue({ running: true, status: "restarting", health: "starting", name: "desktop" });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

test("viewer connection events update resolution and reconnecting preserves an unsent instruction", async () => {
  render(<Home />);
  await waitFor(() => expect(connections).toHaveLength(1));
  act(() => connections[0].dispatchEvent(new Event("connect")));
  expect(await screen.findByText(/Desktop 1280x800/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "再接続" })).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "指示" }), { target: { value: "保存する文書" } });
  act(() => connections[0].dispatchEvent(new Event("disconnect")));
  fireEvent.click(await screen.findByRole("button", { name: "再接続" }));
  await waitFor(() => expect(connections).toHaveLength(2));
  expect(connections[1].viewOnly).toBe(true);
  expect(screen.getByRole("textbox", { name: "指示" })).toHaveValue("保存する文書");
  expect(createTask).not.toHaveBeenCalled();
});

test("debug restart results reach the workspace log through the actual controls", async () => {
  render(<Home />);
  await screen.findByRole("button", { name: "作業環境を停止" });
  fireEvent.click(screen.getByRole("button", { name: /VM管理（デバッグ）/ }));
  fireEvent.click(screen.getByRole("button", { name: "VM再起動" }));
  expect(restartVm).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "再起動する" }));
  expect(await screen.findByText("VM再起動開始")).toBeVisible();
  expect(await screen.findByText("VM再起動指示OK: desktop")).toBeVisible();
  await waitFor(() => expect(screen.getByRole("button", { name: "VM再起動" })).toBeEnabled());
  expect(restartVm).toHaveBeenCalledOnce();
});

test("the initial completed task can be restored through HTTP before any socket snapshot", async () => {
  vi.mocked(getCurrentTask).mockResolvedValueOnce({ session_id: "saved", vm_id: "desktop", state: "completed",
    is_running: false, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0 });
  vi.mocked(getTaskHistory).mockResolvedValue([archived]);
  vi.mocked(getTaskDetail).mockResolvedValueOnce(archived);
  render(<Home />);
  expect(await screen.findByText("タスク: 保存済みの作業（完了）")).toBeVisible();
  expect(screen.getByTitle("保存済みの作業").closest(".history-item")).toHaveClass("selected");
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
});

test("an initial restoration failure is visible after environment discovery completes", async () => {
  let fail!: (error: Error) => void;
  vi.mocked(getCurrentTask).mockReturnValueOnce(new Promise((_, reject) => { fail = reject; }));
  render(<Home />);
  await screen.findByRole("button", { name: "作業環境を停止" });
  await act(async () => fail(new Error("history unavailable")));
  expect(await screen.findByText("状態の復元に失敗")).toBeVisible();
});

test.each([new Error("作成結果を確認できません"), "作成結果を確認できません"])(
  "creation restoration errors remain visible when no environment has been selected: %s", async error => {
    vmCreationRetry(null, "container");
    vi.mocked(getVms).mockReturnValueOnce(new Promise(() => {}));
    vi.mocked(createVm).mockRejectedValueOnce(error);
    render(<Home />);
    expect(await screen.findByText("作成結果の確認: 作成結果を確認できません")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "指示" })).toBeDisabled();
    expect(createTask).not.toHaveBeenCalled();
  },
);
