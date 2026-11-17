import { act, render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import Home from "./page";
import { ApiError, controlTask, createTask, createVm, deleteTask, getCurrentTask, getTaskDetail, getTaskHistory, getVms, login } from "@/lib/api";
import { vmCreationRetry } from "@/lib/vm-creation-retries";
import type { TaskDetail, TaskHistoryItem, TaskStatus, VmInfo, WsMessage } from "@/lib/types";

vi.mock("@/components/VncViewer", () => ({ default: () => null }));
vi.mock("@/components/VMControls", () => ({ default: () => null }));
const ws = vi.hoisted(() => ({ handler: null as null | ((message: import("@/lib/types").WsMessage) => void) }));
const current = vi.hoisted(() => ({ running: true }));
vi.mock("@/hooks/useWebSocket", () => ({ useWebSocket: (handler: typeof ws.handler) => { ws.handler = handler; } }));
vi.mock("@/lib/api", () => ({
  ApiError: class extends Error { constructor(public readonly status: number, message: string) { super(message); } },
  getAuthStatus: async () => ({ authenticated: true }),
  login: vi.fn(), logout: vi.fn(),
  getVms: vi.fn(async () => [{ id: "desktop", name: "desktop", status: "running", kind: "container", ws_port: 6081 }]),
  getCurrentTask: vi.fn(async () => ({ session_id: current.running ? "running" : null, vm_id: "desktop", state: current.running ? "executing" : "idle", is_running: current.running, subtasks: [], current_subtask_index: 0 })),
  getTaskHistory: vi.fn(async () => [{ id: "old", vm_id: "desktop", instruction: "old completed task", state: "completed", updated_at: 0 }]),
  getTaskDetail: vi.fn(async () => ({ id: "old", vm_id: "desktop", instruction: "old completed task", state: "completed", subtasks: [], actions: [], current_subtask_index: 0, updated_at: 0 })),
  getVncWsUrl: () => "ws://localhost:6081",
  controlTask: vi.fn(), createTask: vi.fn(), deleteTask: vi.fn(),
  createVm: vi.fn(), deleteVm: vi.fn(), stopVm: vi.fn(), startVm: vi.fn(),
}));

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  current.running = true;
  vi.mocked(login).mockResolvedValue({ authenticated: true });
});

test.each(["starting", "unhealthy"])("task input waits for VM readiness: %s", async (initialHealth) => {
  current.running = false;
  let health = initialHealth;
  let poll: (() => void) | undefined;
  const previous = vi.mocked(getVms).getMockImplementation();
  const interval = globalThis.setInterval;
  const clock = vi.spyOn(globalThis, "setInterval").mockImplementation((callback, delay, ...args) => {
    if (delay === 15000) poll = callback as () => void;
    return interval(callback, delay, ...args);
  });
  vi.mocked(getVms).mockImplementation(async () => [{ id: "desktop", name: "desktop", status: "running",
    health, kind: "container", managed: false, vnc_port: 0, ws_port: 0, vnc_host: "desktop" }]);
  try {
    render(<Home />);
    const input = await screen.findByRole("textbox", { name: "指示" });
    await screen.findByText(initialHealth === "starting" ? /コンテナ・準備中/ : /コンテナ・接続異常/);
    expect(input).toBeDisabled();
    expect(createTask).not.toHaveBeenCalled();
    health = "healthy";
    await act(async () => poll?.());
    await waitFor(() => expect(input).toBeEnabled());
  } finally {
    clock.mockRestore();
    if (previous) vi.mocked(getVms).mockImplementation(previous);
  }
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

test("a pending VM creation is restored without changing the selected environment", async () => {
  vmCreationRetry("作成中の環境", "container");
  const created = deferred<VmInfo>();
  vi.mocked(createVm).mockReturnValueOnce(created.promise);
  render(<Home />);
  await waitFor(() => expect(createVm).toHaveBeenCalledWith("作成中の環境", "container", expect.any(AbortSignal)));
  for (const button of await screen.findAllByRole("button", { name: /作成中/ })) expect(button).toBeDisabled();
  await act(async () => created.resolve({ id: "desk-new", name: "作成中の環境", status: "creating", health: null, kind: "container", managed: true, vnc_host: "desk-new", vnc_port: 0, ws_port: 0 }));
  await waitFor(() => expect(screen.queryAllByRole("button", { name: /作成中\.\.\./ })).toHaveLength(0));
  expect(screen.getByText("desktop", { selector: ".vm-tab-name" }).closest(".vm-tab")).toHaveClass("selected");
  expect(getVms).toHaveBeenCalled();
});

function detail(id: string): TaskDetail {
  return { id, vm_id: "desktop", instruction: id, state: "completed", success: true,
    action_count: 0, success_count: 0, failure_count: 0, subtasks: [], actions: [],
    current_subtask_index: 0, updated_at: 0, created_at: 0, goal: {} };
}

test("a restarted backend can restore lower task orders and release old manual input", async () => {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({ type: "state", server_id: "before", session_id: "running", session_order: 50,
    vm_id: "desktop", state: "awaiting_user", revision: 9, action_count: 0, subtask_index: 0, subtask_count: 0,
    manual_input: { id: "old-consent", reason: "再起動前の入力待ち" } }));
  expect(await screen.findByText("再起動前の入力待ち")).toBeVisible();
  act(() => ws.handler?.({ type: "snapshot", server_id: "after", through_session_order: 1, tasks: [{
    server_id: "after", session_id: "restored", session_order: 1, vm_id: "desktop", state: "completed",
    is_running: false, action_count: 0, success_count: 0, failure_count: 0, subtasks: [], current_subtask_index: 0,
  }] }));
  await waitFor(() => expect(screen.queryByText("再起動前の入力待ち")).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled();
});

test("a new backend's event before its snapshot can restore a lower-order live task", async () => {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({ type: "state", server_id: "before", session_id: "running", session_order: 50,
    vm_id: "desktop", state: "executing", action_count: 0, subtask_index: 0, subtask_count: 0 }));
  act(() => ws.handler?.({ type: "state", server_id: "after", session_id: "restored", session_order: 1,
    vm_id: "desktop", state: "awaiting_user", action_count: 0, subtask_index: 0, subtask_count: 0,
    manual_input: { id: "new-consent", reason: "再起動後の入力待ち" } }));
  expect(await screen.findByText("再起動後の入力待ち")).toBeVisible();
  act(() => ws.handler?.({ type: "snapshot", server_id: "after", through_session_order: 0, tasks: [] }));
  expect(screen.getByText("再起動後の入力待ち")).toBeVisible();
});

test("a delayed response from a retired backend cannot replace new live state or clear the draft", async () => {
  current.running = false;
  const response = deferred<TaskStatus>();
  vi.mocked(createTask).mockReturnValueOnce(response.promise);
  render(<Home />);
  const input = await screen.findByRole("textbox", { name: "指示" });
  await waitFor(() => expect(input).toBeEnabled());
  act(() => ws.handler?.({ type: "snapshot", server_id: "before", through_session_order: 50, tasks: [] }));
  fireEvent.change(input, { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  await waitFor(() => expect(createTask).toHaveBeenCalledOnce());
  act(() => ws.handler?.({ type: "state", server_id: "after", session_id: "restored", session_order: 1,
    vm_id: "desktop", state: "awaiting_user", action_count: 0, subtask_index: 0, subtask_count: 0,
    manual_input: { id: "new-consent", reason: "新しいサーバーの入力待ち" } }));
  await act(async () => response.resolve({ server_id: "before", session_id: "late", session_order: 999,
    vm_id: "desktop", state: "awaiting_user", is_running: true, action_count: 0, success_count: 0,
    failure_count: 0, subtasks: [], current_subtask_index: 0,
    manual_input: { id: "old-consent", reason: "古い応答" } }));
  expect(screen.getByText("新しいサーバーの入力待ち")).toBeVisible();
  expect(input).toHaveValue("報告書を保存する");
  expect(screen.queryByText("古い応答")).not.toBeInTheDocument();
});

test("a delayed control refresh cannot introduce an old backend that was never observed", async () => {
  const response = deferred<TaskStatus>();
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  vi.mocked(getCurrentTask).mockReturnValueOnce(response.promise);
  vi.mocked(controlTask).mockRejectedValueOnce(new ApiError(409, "対象が変わりました"));
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  await waitFor(() => expect(getCurrentTask).toHaveBeenCalledWith("desktop"));
  act(() => ws.handler?.({ type: "state", server_id: "new-backend", session_id: "restored", session_order: 1,
    vm_id: "desktop", state: "awaiting_user", action_count: 0, subtask_index: 0, subtask_count: 0,
    manual_input: { id: "new-consent", reason: "新しいサーバーの入力待ち" } }));
  await act(async () => response.resolve({ server_id: "old-unseen-backend", session_id: "late", session_order: 999,
    vm_id: "desktop", state: "completed", is_running: false, action_count: 0, success_count: 0,
    failure_count: 0, subtasks: [], current_subtask_index: 0 }));
  expect(screen.getByText("新しいサーバーの入力待ち")).toBeVisible();
  expect(screen.getByRole("button", { name: "画面を送信して再開" })).toBeEnabled();
});

test("retired backend events and snapshots cannot undo current state", async () => {
  current.running = false;
  render(<Home />);
  await screen.findByRole("button", { name: /desktop.*起動中/ });
  act(() => ws.handler?.({ type: "snapshot", server_id: "before", tasks: [] }));
  act(() => ws.handler?.({ type: "state", server_id: "after", session_id: "new", session_order: 1,
    vm_id: "desktop", state: "awaiting_user", action_count: 0, subtask_index: 0, subtask_count: 0,
    manual_input: { id: "current", reason: "現在の入力待ち" } }));
  await screen.findByText("現在の入力待ち");
  act(() => {
    ws.handler?.({ type: "complete", server_id: "before", session_id: "obsolete", session_order: 999,
      vm_id: "desktop", revision: 99, success: true });
    ws.handler?.({ type: "action", server_id: "before", session_id: "obsolete", session_order: 999,
      vm_id: "desktop", action_type: "wait", description: "obsolete action", success: true });
    ws.handler?.({ type: "error", server_id: "before", session_id: "obsolete", session_order: 999,
      vm_id: "desktop", message: "obsolete error" });
    ws.handler?.({ type: "snapshot", server_id: "before", through_session_order: 999, tasks: [] });
    ws.handler?.({ type: "complete", session_id: "unversioned", session_order: 999,
      vm_id: "desktop", revision: 99, success: true });
  });
  expect(screen.getByText("現在の入力待ち")).toBeVisible();
  expect(screen.queryByText(/obsolete/)).not.toBeInTheDocument();
});

test("a server restart preserves the chosen environment and an unsent instruction", async () => {
  current.running = false;
  const vms = ["desktop", "second"].map(id => ({ id, name: id, status: "running", kind: "container" } as VmInfo));
  vi.mocked(getVms).mockResolvedValueOnce(vms).mockResolvedValueOnce(vms).mockResolvedValueOnce(vms);
  render(<Home />);
  fireEvent.click(await screen.findByRole("button", { name: /second.*起動中/ }));
  fireEvent.change(screen.getByRole("textbox", { name: "指示" }), { target: { value: "売上を集計してください" } });
  act(() => ws.handler?.({ type: "snapshot", server_id: "before", tasks: [] }));
  act(() => ws.handler?.({ type: "snapshot", server_id: "after", through_session_order: 0, tasks: [] }));
  await waitFor(() => expect(getVms).toHaveBeenCalledTimes(3));
  expect(screen.getByRole("button", { name: /second.*起動中/ }).closest(".vm-tab")).toHaveClass("selected");
  expect(screen.getByRole("textbox", { name: "指示" })).toHaveValue("売上を集計してください");
});

test("a server restart reloads the selected history and ignores its old pending response", async () => {
  current.running = false;
  const slow = deferred<TaskDetail>();
  vi.mocked(getTaskDetail).mockReturnValueOnce(slow.promise).mockResolvedValueOnce({
    ...detail("restored history"), vm_id: "archived",
  });
  render(<Home />);
  await screen.findByRole("button", { name: /desktop.*起動中/ });
  act(() => ws.handler?.({ type: "snapshot", server_id: "before", tasks: [] }));
  fireEvent.click(await screen.findByTitle("old completed task"));
  act(() => ws.handler?.({ type: "snapshot", server_id: "after", through_session_order: 0, tasks: [] }));
  await screen.findByText("タスク: restored history（完了）");
  await act(async () => slow.resolve(detail("stale history")));
  expect(screen.getByText("タスク: restored history（完了）")).toBeVisible();
  expect(screen.queryByText("タスク: stale history（完了）")).not.toBeInTheDocument();
});

test("a submission response can detect a restart before the WebSocket reconnects", async () => {
  current.running = false;
  vi.mocked(createTask).mockResolvedValueOnce({ server_id: "after", session_id: "new", session_order: 1,
    vm_id: "desktop", state: "executing", is_running: true, action_count: 0, success_count: 0,
    failure_count: 0, subtasks: [], current_subtask_index: 0 });
  render(<Home />);
  const input = await screen.findByRole("textbox", { name: "指示" });
  await waitFor(() => expect(input).toBeEnabled());
  act(() => ws.handler?.({ type: "state", server_id: "before", session_id: "old", session_order: 50,
    vm_id: "desktop", state: "completed", action_count: 0, subtask_index: 0, subtask_count: 0 }));
  fireEvent.change(input, { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  await waitFor(() => expect(input).toHaveValue(""));
  expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
});

test("an old backend's 410 cannot force a new retry key after a restart", async () => {
  current.running = false;
  let reject!: (error: Error) => void;
  vi.mocked(createTask).mockReturnValueOnce(new Promise((_, fail) => { reject = fail; }))
    .mockRejectedValueOnce(new ApiError(410, "新しい接続先でも受付済み"));
  render(<Home />);
  const input = await screen.findByRole("textbox", { name: "指示" });
  await waitFor(() => expect(input).toBeEnabled());
  act(() => ws.handler?.({ type: "snapshot", server_id: "before", tasks: [] }));
  fireEvent.change(input, { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  await waitFor(() => expect(createTask).toHaveBeenCalledOnce());
  act(() => ws.handler?.({ type: "snapshot", server_id: "after", through_session_order: 0, tasks: [] }));
  await act(async () => reject(new ApiError(410, "古い接続先では削除済み")));
  fireEvent.click(await screen.findByRole("button", { name: "再送" }));
  await screen.findByRole("button", { name: "新しいタスクとして実行" });
  expect(vi.mocked(createTask).mock.calls[1][3]).toBe(vi.mocked(createTask).mock.calls[0][3]);
});

test("reconnecting to the same backend keeps the task-order fence", async () => {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({ type: "state", server_id: "same", session_id: "running", session_order: 50,
    vm_id: "desktop", state: "awaiting_user", action_count: 0, subtask_index: 0, subtask_count: 0,
    manual_input: { id: "current", reason: "現在の入力待ち" } }));
  act(() => ws.handler?.({ type: "snapshot", server_id: "same", through_session_order: 1, tasks: [] }));
  expect(screen.getByText("現在の入力待ち")).toBeVisible();
  expect(screen.getByRole("button", { name: "画面を送信して再開" })).toBeEnabled();
});

test("an abandoned workspace cannot start fresh requests from a late initial status", async () => {
  const response = deferred<TaskStatus>();
  vi.mocked(getCurrentTask).mockReturnValueOnce(response.promise);
  const { unmount } = render(<Home />);
  await screen.findByRole("button", { name: /desktop.*起動中/ });
  unmount();
  const calls = vi.mocked(getVms).mock.calls.length;
  await act(async () => response.resolve({ server_id: "late", session_id: null, state: "idle", is_running: false,
    action_count: 0, success_count: 0, failure_count: 0, subtasks: [], current_subtask_index: 0 }));
  expect(getVms).toHaveBeenCalledTimes(calls);
});

test("an abandoned submission cannot start fresh requests from a new backend response", async () => {
  current.running = false;
  const response = deferred<TaskStatus>();
  vi.mocked(createTask).mockReturnValueOnce(response.promise);
  const { unmount } = render(<Home />);
  const input = await screen.findByRole("textbox", { name: "指示" });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.change(input, { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  unmount();
  const calls = vi.mocked(getVms).mock.calls.length;
  await act(async () => response.resolve({ server_id: "late", session_id: "task", state: "executing", is_running: true,
    action_count: 0, success_count: 0, failure_count: 0, subtasks: [], current_subtask_index: 0 }));
  expect(getVms).toHaveBeenCalledTimes(calls);
});

test.each([false, true])("VM polling preserves archived history (environment initially present: %s)", async (present) => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  try {
    current.running = false;
    const desktop = { id: "desktop", name: "desktop", status: "running", kind: "container" } as VmInfo;
    const archived = { id: "archived", name: "archived", status: "running", kind: "container" } as VmInfo;
    vi.mocked(getVms).mockResolvedValueOnce(present ? [desktop, archived] : [desktop]).mockResolvedValueOnce([desktop]);
    vi.mocked(getTaskDetail).mockResolvedValueOnce({ ...detail("archived task"), vm_id: "archived" });
    render(<Home />);
    await screen.findByRole("button", { name: /desktop.*起動中/ });
    fireEvent.click(await screen.findByTitle("old completed task"));
    await screen.findByText("タスク: archived task（完了）");
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(screen.getByText("タスク: archived task（完了）")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "指示" })).toBeDisabled();
  } finally { vi.useRealTimers(); }
});

test("a late initial VM list cannot switch away from an archived history selection", async () => {
  current.running = false;
  const vms = deferred<VmInfo[]>();
  vi.mocked(getVms).mockReturnValueOnce(vms.promise);
  vi.mocked(getTaskDetail).mockResolvedValueOnce({ ...detail("archived task"), vm_id: "archived" });
  render(<Home />);
  fireEvent.click(await screen.findByTitle("old completed task"));
  await screen.findByText("タスク: archived task（完了）");
  await act(async () => vms.resolve([{ id: "desktop", name: "desktop", status: "running", kind: "container" } as VmInfo]));
  expect(screen.getByText("タスク: archived task（完了）")).toBeVisible();
  expect(screen.getByRole("textbox", { name: "指示" })).toBeDisabled();
});

test("legacy history selected before the first VM list remains readable", async () => {
  current.running = false;
  const vms = deferred<VmInfo[]>();
  vi.mocked(getVms).mockReturnValueOnce(vms.promise);
  vi.mocked(getTaskDetail).mockResolvedValueOnce({ ...detail("legacy"), vm_id: null });
  render(<Home />);
  fireEvent.click(await screen.findByTitle("old completed task"));
  await screen.findByText("タスク: legacy（完了）");
  await act(async () => vms.resolve([{ id: "desktop", name: "desktop", status: "running", kind: "container" } as VmInfo]));
  expect(screen.getByText("タスク: legacy（完了）")).toBeVisible();
});

test("VM polling still replaces a removed live environment when no history is open", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  try {
    current.running = false;
    const desktop = { id: "desktop", name: "desktop", status: "running", kind: "container" } as VmInfo;
    const second = { ...desktop, id: "second", name: "second" };
    vi.mocked(getVms).mockResolvedValueOnce([desktop, second]).mockResolvedValueOnce([second]);
    render(<Home />);
    const first = await screen.findByRole("button", { name: /desktop.*起動中/ });
    expect(first.closest(".vm-tab")).toHaveClass("selected");
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(screen.getByRole("button", { name: /second.*起動中/ }).closest(".vm-tab")).toHaveClass("selected");
    expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled();
  } finally { vi.useRealTimers(); }
});

test("choosing a live environment after archived history clears the history selection", async () => {
  current.running = false;
  vi.mocked(getTaskDetail).mockResolvedValueOnce({ ...detail("archived task"), vm_id: "archived" });
  render(<Home />);
  await screen.findByRole("button", { name: /desktop.*起動中/ });
  fireEvent.click(await screen.findByTitle("old completed task"));
  await screen.findByText("タスク: archived task（完了）");
  fireEvent.click(screen.getByRole("button", { name: /desktop.*起動中/ }));
  expect(screen.queryByText("タスク: archived task（完了）")).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled();
});

test("controls carry the identity of the live task even while viewing history", async () => {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  fireEvent.click(await screen.findByTitle("old completed task"));
  await screen.findByText("タスク: old completed task（完了）");
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  expect(controlTask).toHaveBeenCalledWith("stop", "desktop", "running", undefined);
});

test("a stale control refreshes the live task without retrying the operation", async () => {
  const latest = { session_id: "replacement", vm_id: "desktop", state: "awaiting_user", is_running: true,
    revision: 2, session_order: 2, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0,
    manual_input: { id: "replacement-consent", reason: "新しいタスクの入力を待っています" } };
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  vi.mocked(getCurrentTask).mockResolvedValueOnce(latest);
  vi.mocked(controlTask).mockRejectedValueOnce(new ApiError(409, "操作対象のタスクが切り替わりました"));
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  expect(await screen.findByText("新しいタスクの入力を待っています")).toBeVisible();
  expect(controlTask).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "画面を送信して再開" })).toBeEnabled();
});

test("a live state without a task identity cannot send a control", async () => {
  current.running = false;
  render(<Home />);
  await screen.findByRole("button", { name: /desktop.*起動中/ });
  act(() => ws.handler?.({ type: "state", vm_id: "desktop", state: "executing",
    action_count: 0, subtask_index: 0, subtask_count: 0 }));
  await screen.findByText("EXECUTING");
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  expect(controlTask).not.toHaveBeenCalled();
});

test("a control conflict releases controls when the task disappeared", async () => {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({ type: "state", session_id: "running", session_order: 1,
    vm_id: "desktop", state: "executing", action_count: 0, subtask_index: 0, subtask_count: 0 }));
  vi.mocked(getCurrentTask).mockResolvedValueOnce({ session_id: null, vm_id: "desktop", state: "idle",
    is_running: false, session_order: 0, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0 });
  vi.mocked(controlTask).mockRejectedValueOnce(new ApiError(409, "タスクがありません"));
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeDisabled());
  expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled();
});

test.each([null, "running"])("a late control refresh for %s preserves a newer live task", async (id) => {
  const refresh = deferred<TaskStatus>();
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  vi.mocked(getCurrentTask).mockReturnValueOnce(refresh.promise);
  vi.mocked(controlTask).mockRejectedValueOnce(new ApiError(409, "操作対象のタスクが切り替わりました"));
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  await waitFor(() => expect(getCurrentTask).toHaveBeenCalledWith("desktop"));
  act(() => ws.handler?.({ type: "state", session_id: "replacement", session_order: 2,
    vm_id: "desktop", state: "awaiting_user", revision: 9, action_count: 0, subtask_index: 0, subtask_count: 0,
    manual_input: { id: "replacement-consent", reason: "新しいタスクの入力待ち" } }));
  await act(async () => refresh.resolve({ session_id: id, session_order: id ? 1 : 0,
    vm_id: "desktop", state: id ? "completed" : "idle", is_running: false,
    action_count: 0, success_count: 0, failure_count: 0, subtasks: [], current_subtask_index: 0 }));
  expect(screen.getByText("新しいタスクの入力待ち")).toBeVisible();
  expect(screen.getByRole("button", { name: "画面を送信して再開" })).toBeEnabled();
  expect(controlTask).toHaveBeenCalledTimes(1);
});

test("the last selected history wins when details arrive out of order", async () => {
  const slow = deferred<TaskDetail>();
  vi.mocked(getTaskHistory).mockResolvedValueOnce([detail("old"), detail("newer")]);
  vi.mocked(getTaskDetail).mockReturnValueOnce(slow.promise).mockResolvedValueOnce(detail("newer"));
  render(<Home />);
  fireEvent.click(await screen.findByTitle("old"));
  fireEvent.click(screen.getByTitle("newer"));
  await screen.findByText("タスク: newer（完了）");
  await act(async () => slow.resolve(detail("old")));
  expect(screen.getByText("タスク: newer（完了）")).toBeVisible();
  expect(screen.queryByText("タスク: old（完了）")).not.toBeInTheDocument();
});

test("a pending history request cannot switch back from the VM the user selected", async () => {
  const slow = deferred<TaskDetail>();
  vi.mocked(getTaskDetail).mockReturnValueOnce(slow.promise);
  vi.mocked(getVms).mockResolvedValueOnce([
    { id: "desktop", name: "desktop", status: "running", kind: "container" } as VmInfo,
    { id: "second", name: "second", status: "running", kind: "container" } as VmInfo,
  ]);
  render(<Home />);
  fireEvent.click(await screen.findByTitle("old completed task"));
  const second = screen.getByRole("button", { name: /second.*起動中/ });
  fireEvent.click(second);
  await act(async () => slow.resolve(detail("old")));
  expect(second.closest(".vm-tab")).toHaveClass("selected");
  expect(screen.queryByText("タスク: old（完了）")).not.toBeInTheDocument();
});

test("legacy history without a VM uses the initial environment once its list arrives", async () => {
  current.running = false;
  const vms = deferred<VmInfo[]>();
  const history = deferred<TaskDetail>();
  vi.mocked(getVms).mockReturnValueOnce(vms.promise);
  vi.mocked(getTaskDetail).mockReturnValueOnce(history.promise);
  render(<Home />);
  fireEvent.click(await screen.findByTitle("old completed task"));
  await act(async () => vms.resolve([
    { id: "desktop", name: "desktop", status: "running", kind: "container" } as VmInfo,
  ]));
  await act(async () => history.resolve({ ...detail("legacy"), vm_id: null }));
  expect(screen.getByText("タスク: legacy（完了）")).toBeVisible();
});

test("an older history refresh cannot resurrect a removed entry", async () => {
  const slow = deferred<TaskHistoryItem[]>();
  vi.mocked(getTaskHistory).mockReturnValueOnce(slow.promise).mockResolvedValueOnce([]);
  render(<Home />);
  await waitFor(() => expect(getTaskHistory).toHaveBeenCalledTimes(1));
  act(() => ws.handler?.({ type: "snapshot", tasks: [] }));
  await screen.findByText("履歴なし");
  await act(async () => slow.resolve([detail("removed")]));
  expect(screen.queryByTitle("removed")).not.toBeInTheDocument();
});

test("finishing a history deletion does not clear a newer selection", async () => {
  const deletion = deferred<{ status: string }>();
  vi.mocked(getTaskHistory).mockResolvedValueOnce([detail("old"), detail("newer")]);
  vi.mocked(getTaskDetail).mockResolvedValueOnce(detail("old")).mockResolvedValueOnce(detail("newer"));
  vi.mocked(deleteTask).mockReturnValueOnce(deletion.promise);
  render(<Home />);
  fireEvent.click(await screen.findByTitle("old"));
  await screen.findByText("タスク: old（完了）");
  fireEvent.click(screen.getByRole("button", { name: "oldを削除" }));
  fireEvent.click(screen.getByTitle("newer"));
  await screen.findByText("タスク: newer（完了）");
  await act(async () => deletion.resolve({ status: "deleted" }));
  expect(screen.getByText("タスク: newer（完了）")).toBeVisible();
});

test("manual input survives reconnect and sends consent for only the current request", async () => {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({ type: "snapshot", tasks: [{
    session_id: "running", vm_id: "desktop", state: "awaiting_user", is_running: true,
    action_count: 1, success_count: 1, failure_count: 0, subtasks: [], current_subtask_index: 0,
    manual_input: { id: "current-request", reason: "ログイン情報を入力してください" },
  }] }));
  expect(await screen.findByText("ログイン情報を入力してください")).toBeVisible();
  expect(screen.getByRole("textbox")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "画面を送信して再開" }));
  expect(controlTask).toHaveBeenCalledWith("resume", "desktop", "running", "current-request");
  act(() => ws.handler?.({ type: "state", session_id: "running", vm_id: "desktop", state: "executing", revision: 9,
    action_count: 2, subtask_index: 0, subtask_count: 1, manual_input: null }));
  await waitFor(() => expect(screen.queryByText("ログイン情報を入力してください")).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "再開" })).toBeDisabled();
});

test("history keeps live controls available and receives live completion", async () => {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  fireEvent.click(await screen.findByTitle("old completed task"));
  await screen.findByText("COMPLETED");
  expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  expect(screen.getByRole("textbox")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  expect(controlTask).toHaveBeenCalledWith("stop", "desktop", "running", undefined);
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

test("a previous task cannot complete the task now running on the same VM", async () => {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({
    type: "state", session_id: "new-task", vm_id: "desktop", state: "executing", revision: 3,
    session_order: 2, subtask_index: 0, subtask_count: 1, action_count: 0,
  } as WsMessage));
  act(() => ws.handler?.({
    type: "complete", session_id: "running", vm_id: "desktop", success: true,
    session_order: 1, revision: 20,
  } as WsMessage));
  expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  expect(screen.getByRole("textbox")).toBeDisabled();
  expect(screen.queryByText("タスク完了")).not.toBeInTheDocument();
});

test("an empty reconnect snapshot clears a task deleted while disconnected", async () => {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({ type: "snapshot", tasks: [] }));
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  expect(screen.getByRole("textbox")).toBeEnabled();
});

test("reconnect releases a deleted running task even when older history remains", async () => {
  vi.mocked(getCurrentTask).mockResolvedValueOnce({
    session_id: "removed", vm_id: "desktop", state: "executing", is_running: true,
    session_order: 2, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0,
  });
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({ type: "snapshot", through_session_order: 2, tasks: [{
    session_id: "old", vm_id: "desktop", state: "completed", is_running: false,
    session_order: 1, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0,
  }] }));
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  expect(screen.getByRole("textbox")).toBeEnabled();
});

test.each(["executing", "completed"])("reconnect replaces a deleted %s task with surviving history", async state => {
  vi.mocked(getCurrentTask).mockResolvedValueOnce({
    session_id: "removed", vm_id: "desktop", state: "executing", is_running: true,
    session_order: 2, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0,
  });
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  if (state === "completed") act(() => ws.handler?.({
    type: "complete", session_id: "removed", vm_id: "desktop", session_order: 2, success: true,
  }));
  act(() => ws.handler?.({ type: "snapshot", through_session_order: 2, tasks: [{
    session_id: "old", vm_id: "desktop", state: "completed", is_running: false,
    session_order: 1, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [{ id: "remaining", description: "削除後に残っている作業" }], current_subtask_index: 0,
  }] }));
  expect(screen.getByText("削除後に残っている作業")).toBeVisible();
  expect(document.getElementById("state-badge")).toHaveTextContent("COMPLETED");
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  // The displayed older task must not lower the fence against deleted-task events.
  act(() => ws.handler?.({ type: "state", session_id: "removed", vm_id: "desktop", session_order: 2,
    revision: 99, state: "executing", action_count: 0, subtask_index: 0, subtask_count: 0 }));
  act(() => ws.handler?.({ type: "snapshot", through_session_order: 2, tasks: [{
    session_id: "removed", vm_id: "desktop", state: "executing", is_running: true,
    session_order: 2, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0,
  }] }));
  expect(screen.getByText("削除後に残っている作業")).toBeVisible();
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  act(() => ws.handler?.({ type: "state", session_id: "new", vm_id: "desktop", session_order: 3,
    state: "executing", action_count: 0, subtask_index: 0, subtask_count: 0 }));
  expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  expect(controlTask).toHaveBeenCalledWith("stop", "desktop", "new", undefined);
});

test("a snapshot started before a completed task cannot replace it with older history", async () => {
  vi.mocked(getCurrentTask).mockResolvedValueOnce({
    session_id: "new", vm_id: "desktop", state: "executing", is_running: true,
    session_order: 2, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0,
  });
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({ type: "complete", session_id: "new", vm_id: "desktop", session_order: 2, success: true }));
  act(() => ws.handler?.({ type: "snapshot", through_session_order: 1, tasks: [{
    session_id: "old", vm_id: "desktop", state: "failed", is_running: false,
    session_order: 1, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [{ id: "stale", description: "古い同期結果" }], current_subtask_index: 0,
  }] }));
  expect(document.getElementById("state-badge")).toHaveTextContent("COMPLETED");
  expect(screen.queryByText("古い同期結果")).not.toBeInTheDocument();
});

test("a snapshot started before a new task cannot clear that task", async () => {
  vi.mocked(getCurrentTask).mockResolvedValueOnce({
    session_id: "new", vm_id: "desktop", state: "executing", is_running: true,
    session_order: 2, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0,
  });
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({ type: "snapshot", through_session_order: 1, tasks: [] }));
  expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  expect(screen.getByRole("textbox")).toBeDisabled();
});

test("persisted active state with no running worker does not lock the controls", async () => {
  vi.mocked(getCurrentTask).mockResolvedValueOnce({
    session_id: "running", vm_id: "desktop", state: "executing", is_running: true,
    session_order: 1, revision: 8, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0,
  });
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({ type: "snapshot", through_session_order: 1, tasks: [{
    session_id: "running", vm_id: "desktop", state: "executing", is_running: false,
    session_order: 1, revision: 2, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0,
  }] }));
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  expect(screen.getByRole("textbox")).toBeEnabled();
  act(() => ws.handler?.({ type: "state", session_id: "running", vm_id: "desktop",
    session_order: 1, revision: 9, state: "executing", action_count: 0,
    subtask_index: 0, subtask_count: 0 }));
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
});

test("late initial restoration cannot replace a live WebSocket task with history", async () => {
  let restore!: (value: TaskStatus) => void;
  vi.mocked(getCurrentTask).mockReturnValueOnce(new Promise((resolve) => { restore = resolve; }));
  render(<Home />);
  await screen.findByRole("textbox");
  act(() => ws.handler?.({ type: "snapshot", tasks: [{
    session_id: "new-task", vm_id: "desktop", state: "executing", is_running: true,
    action_count: 0, success_count: 0, failure_count: 0, subtasks: [], current_subtask_index: 0,
  }] }));
  await act(async () => restore({
    session_id: "old", vm_id: "desktop", state: "completed", is_running: false,
    action_count: 0, success_count: 0, failure_count: 0, subtasks: [], current_subtask_index: 0,
  }));
  expect(screen.queryByText("COMPLETED")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  expect(screen.getByRole("textbox")).toBeDisabled();
});

test("the first snapshot restores completed history when the initial HTTP request is slow", async () => {
  vi.mocked(getCurrentTask).mockReturnValueOnce(new Promise(() => {}));
  render(<Home />);
  await screen.findByRole("textbox");
  act(() => ws.handler?.({ type: "snapshot", tasks: [{
    session_id: "old", vm_id: "desktop", state: "completed", is_running: false,
    session_order: 2, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0,
  }] }));
  expect(await screen.findByText("タスク: old completed task（完了）")).toBeVisible();
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
});

test("an older first snapshot cannot open history over a newer HTTP task", async () => {
  vi.mocked(getCurrentTask).mockResolvedValueOnce({
    session_id: "new", vm_id: "desktop", state: "executing", is_running: true,
    session_order: 2, revision: 3, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0,
  });
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  act(() => ws.handler?.({ type: "snapshot", tasks: [{
    session_id: "old", vm_id: "desktop", state: "completed", is_running: false,
    session_order: 1, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0,
  }] }));
  expect(getTaskDetail).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
});

test("the first snapshot selects the latest task's environment before any user navigation", async () => {
  vi.mocked(getVms).mockResolvedValueOnce([
    { id: "desktop", name: "desktop", status: "running", kind: "container" } as VmInfo,
    { id: "second", name: "second", status: "running", kind: "container" } as VmInfo,
  ]);
  render(<Home />);
  const second = await screen.findByRole("button", { name: /second.*起動中/ });
  act(() => ws.handler?.({ type: "snapshot", tasks: [{
    session_id: "new", vm_id: "second", state: "executing", is_running: true,
    session_order: 2, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0,
  }] }));
  expect(second.closest(".vm-tab")).toHaveClass("selected");
  expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
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

test("a replay restores manual consent from its response even without a WebSocket event", async () => {
  current.running = false;
  vi.mocked(createTask).mockRejectedValueOnce(new Error("response lost")).mockResolvedValueOnce({
    session_id: "accepted", session_order: 2, revision: 8, vm_id: "desktop",
    state: "awaiting_user", is_running: true, replayed: true,
    action_count: 1, success_count: 1, failure_count: 0, current_subtask_index: 0, subtasks: [],
    manual_input: { id: "consent-id", reason: "ログインしてください" },
  });
  render(<Home />);
  const input = await screen.findByRole("textbox", { name: "指示" });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.change(input, { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  fireEvent.click(await screen.findByRole("button", { name: "再送" }));
  expect(await screen.findByText("ログインしてください")).toBeVisible();
  expect(screen.getByText("受付済みのタスクを確認しました")).toBeVisible();
  expect(createTask).toHaveBeenNthCalledWith(2, "報告書を保存する", "desktop", false, vi.mocked(createTask).mock.calls[0][3]);
  fireEvent.click(screen.getByRole("button", { name: "画面を送信して再開" }));
  expect(controlTask).toHaveBeenCalledWith("resume", "desktop", "accepted", "consent-id");
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
