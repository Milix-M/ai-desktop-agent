import { act, render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import Home from "./page";
import { controlTask, getTaskDetail, login } from "@/lib/api";

vi.mock("@/components/VncViewer", () => ({ default: () => null }));
vi.mock("@/components/VMControls", () => ({ default: () => null }));
const ws = vi.hoisted(() => ({ handler: null as null | ((message: import("@/lib/types").WsMessage) => void) }));
const current = vi.hoisted(() => ({ running: true }));
vi.mock("@/hooks/useWebSocket", () => ({ useWebSocket: (handler: typeof ws.handler) => { ws.handler = handler; } }));
vi.mock("@/lib/api", () => ({
  getAuthStatus: async () => ({ authenticated: true }),
  login: vi.fn(), logout: vi.fn(),
  getVms: async () => [{ id: "desktop", name: "desktop", status: "running", kind: "container", ws_port: 6081 }],
  getCurrentTask: async () => ({ session_id: current.running ? "running" : null, vm_id: "desktop", state: current.running ? "executing" : "idle", is_running: current.running, subtasks: [], current_subtask_index: 0 }),
  getTaskHistory: async () => [{ id: "old", vm_id: "desktop", instruction: "old completed task", state: "completed", updated_at: 0 }],
  getTaskDetail: vi.fn(async () => ({ id: "old", vm_id: "desktop", instruction: "old completed task", state: "completed", subtasks: [], actions: [], current_subtask_index: 0, updated_at: 0 })),
  getVncWsUrl: () => "ws://localhost:6081",
  controlTask: vi.fn(), createTask: vi.fn(), deleteTask: vi.fn(),
  createVm: vi.fn(), deleteVm: vi.fn(), stopVm: vi.fn(), startVm: vi.fn(),
}));

afterEach(cleanup);
beforeEach(() => {
  current.running = true;
  vi.mocked(login).mockResolvedValue({ authenticated: true });
});

test("history keeps live controls available and receives live completion", async () => {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  fireEvent.click(await screen.findByTitle("old completed task"));
  await screen.findByText("COMPLETED");
  expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  expect(screen.getByRole("textbox")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  expect(controlTask).toHaveBeenCalledWith("stop", "desktop");
  act(() => ws.handler?.({ type: "complete", vm_id: "desktop", success: true }));
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeDisabled());
  expect(screen.getByRole("textbox")).toBeEnabled();
});

test("reconnect snapshot releases controls after a task finishes offline", async () => {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({ type: "snapshot", tasks: [{
    session_id: "running", vm_id: "desktop", state: "completed", is_running: false,
    action_count: 1, success_count: 1, failure_count: 0, subtasks: [], current_subtask_index: 0,
  }] }));
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeDisabled());
  expect(screen.getByRole("textbox")).toBeEnabled();
});

test("delayed state updates cannot undo a newer completion", async () => {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({ type: "complete", session_id: "running", vm_id: "desktop", success: true, revision: 8 }));
  act(() => ws.handler?.({
    type: "state", session_id: "running", vm_id: "desktop", state: "executing", revision: 4,
    subtask_index: 0, subtask_count: 1, action_count: 1,
  }));
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeDisabled());
  expect(screen.getByRole("textbox")).toBeEnabled();
});

test("reauthentication preserves an unsent instruction while explicit logout clears it", async () => {
  current.running = false;
  render(<Home />);
  const instruction = await screen.findByRole("textbox", { name: "指示" });
  await waitFor(() => expect(instruction).toBeEnabled());
  fireEvent.change(instruction, { target: { value: "売上報告を作成して保存する" } });
  act(() => window.dispatchEvent(new Event("auth-required")));
  fireEvent.change(await screen.findByLabelText("接続キー"), { target: { value: "operator-key" } });
  fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
  expect(await screen.findByRole("textbox", { name: "指示" })).toHaveValue("売上報告を作成して保存する");
  fireEvent.click(screen.getByRole("button", { name: "ログアウト" }));
  fireEvent.change(await screen.findByLabelText("接続キー"), { target: { value: "operator-key" } });
  fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
  expect(await screen.findByRole("textbox", { name: "指示" })).toHaveValue("");
});

test("history restores a limit failure and the usage of all model requests", async () => {
  current.running = false;
  vi.mocked(getTaskDetail).mockResolvedValueOnce({
    id: "old", vm_id: "desktop", instruction: "報告書を保存する", state: "failed",
    success: false, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], actions: [], current_subtask_index: 0, goal: {},
    updated_at: 0, created_at: 0, stop_reason: "token_limit",
    error_message: "トークン使用量の上限に到達しました",
    execution_usage: { requests: 3, input_tokens: 90, output_tokens: 10 },
  });
  render(<Home />);
  fireEvent.click(await screen.findByTitle("old completed task"));
  expect(await screen.findByText("トークン使用量の上限に到達しました")).toBeVisible();
  expect(screen.getByText(/モデル3回（入力90\/出力10トークン）/)).toBeVisible();
});
