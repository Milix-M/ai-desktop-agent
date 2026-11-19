import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import Home from "./page";
import { ApiError, controlTask, getCurrentTask, getTaskHistory, getVms } from "@/lib/api";
import type { TaskStatus, WsMessage } from "@/lib/types";
import { parseTaskStatus } from "@/lib/task-protocol";

class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  send = vi.fn();
  close = vi.fn(() => { this.readyState = 3; this.onclose?.(); });
  constructor() { Socket.instances.push(this); }
  open() { this.readyState = Socket.OPEN; this.onopen?.(); }
  message(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) }); }
}
const viewers: MockRFB[] = [];
class MockRFB extends EventTarget {
  viewOnly = true;
  disconnect = vi.fn();
  constructor() { super(); viewers.push(this); }
}
vi.mock("@novnc/novnc", () => ({ default: MockRFB }));
vi.mock("@/components/VMControls", () => ({ default: () => null }));
vi.mock("@/components/ConnectionPanel", () => ({ default: () => null }));
vi.mock("@/lib/api", () => ({
  ApiError: class extends Error { constructor(public readonly status: number, message: string) { super(message); } },
  getAuthStatus: async () => ({ authenticated: true }),
  confirmAuthentication: vi.fn(), getWsUrl: () => "ws://localhost/ws",
  getVncWsUrl: () => "ws://localhost/vnc/desktop",
  getVms: vi.fn(), getCurrentTask: vi.fn(), getTaskHistory: vi.fn(), getTaskDetail: vi.fn(),
  createVm: vi.fn(), createTask: vi.fn(), controlTask: vi.fn(), deleteTask: vi.fn(),
  deleteVm: vi.fn(), startVm: vi.fn(), stopVm: vi.fn(),
}));
const waiting: TaskStatus = { server_id: "server-1", session_id: "running", session_order: 1,
  vm_id: "desktop", state: "awaiting_user", revision: 3, is_running: true,
  manual_input: { id: "consent-1", reason: "確認用の文字を入力してください" },
  action_count: 1, success_count: 1, failure_count: 0, subtasks: [], current_subtask_index: 0 };
const snapshot = (tasks = [waiting]): WsMessage => ({ type: "snapshot", server_id: "server-1", tasks });

beforeEach(() => {
  vi.resetAllMocks(); sessionStorage.clear(); viewers.length = 0; Socket.instances = [];
  vi.useFakeTimers(); vi.stubGlobal("WebSocket", Socket);
  vi.mocked(getCurrentTask).mockResolvedValue(waiting);
  vi.mocked(getTaskHistory).mockResolvedValue([]);
  vi.mocked(getVms).mockResolvedValue([{ id: "desktop", name: "作業環境", status: "running", health: "healthy",
    managed: false, kind: "container", vnc_host: "desktop", vnc_port: 5900, ws_port: 0 }]);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function mount() {
  await act(async () => { render(<Home />); });
  expect(viewers).toHaveLength(1);
  expect(Socket.instances).toHaveLength(1);
  return Socket.instances[0];
}

test("HTTP restoration and an open status connection cannot enable input before the current snapshot", async () => {
  const socket = await mount();
  expect(screen.getByText(waiting.manual_input!.reason)).toBeVisible();
  expect(screen.getByRole("button", { name: "画面を送信して再開" })).toBeDisabled();
  expect(viewers[0].viewOnly).toBe(true);
  act(() => socket.open());
  expect(viewers[0].viewOnly).toBe(true);
  act(() => socket.message(snapshot()));
  expect(viewers[0].viewOnly).toBe(false);
});

test("a non-Error control rejection preserves the current manual request for an explicit retry", async () => {
  const socket = await mount();
  act(() => { socket.open(); socket.message(snapshot()); });
  vi.mocked(controlTask).mockRejectedValueOnce("接続が途切れました");
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "停止" })); });
  expect(screen.getByText("操作エラー: 接続が途切れました")).toBeVisible();
  expect(screen.getByText(waiting.manual_input!.reason)).toBeVisible();
  expect(viewers[0].viewOnly).toBe(false);
  expect(controlTask).toHaveBeenCalledOnce();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "画面を送信して再開" })); });
  expect(controlTask).toHaveBeenLastCalledWith("resume", "desktop", "running", "consent-1");
});

test("a late control conflict from the retired server cannot refresh or replace new input consent", async () => {
  const socket = await mount();
  act(() => { socket.open(); socket.message(snapshot()); });
  let reject!: (error: Error) => void;
  vi.mocked(controlTask).mockReturnValueOnce(new Promise((_, fail) => { reject = fail; }));
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  const reads = vi.mocked(getCurrentTask).mock.calls.length;
  const replacement = { ...waiting, server_id: "replacement", session_id: "new-task", session_order: 2,
    manual_input: { id: "new-consent", reason: "新しい接続先を確認してください" } };
  act(() => { socket.message({ type: "snapshot", server_id: "replacement", tasks: [replacement] }); });
  await act(async () => { reject(new ApiError(409, "古い接続先の競合")); });
  expect(getCurrentTask).toHaveBeenCalledTimes(reads);
  expect(screen.queryByText(/古い接続先の競合/)).not.toBeInTheDocument();
  expect(screen.getByText(replacement.manual_input.reason)).toBeVisible();
  expect(viewers[0].viewOnly).toBe(false);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "画面を送信して再開" })); });
  expect(controlTask).toHaveBeenLastCalledWith("resume", "desktop", "new-task", "new-consent");
});

test("a control response arriving after unmount cannot issue a conflict refresh or reopen input", async () => {
  const socket = await mount();
  act(() => { socket.open(); socket.message(snapshot()); });
  let reject!: (error: Error) => void;
  vi.mocked(controlTask).mockReturnValueOnce(new Promise((_, fail) => { reject = fail; }));
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  const reads = vi.mocked(getCurrentTask).mock.calls.length;
  cleanup();
  expect(viewers[0].disconnect).toHaveBeenCalledOnce();
  await act(async () => { reject(new ApiError(409, "終了後の応答")); });
  await act(() => vi.advanceTimersByTimeAsync(30000));
  expect(getCurrentTask).toHaveBeenCalledTimes(reads);
  expect(viewers[0].disconnect).toHaveBeenCalledOnce();
  expect(Socket.instances).toHaveLength(1);
  expect(screen.queryByRole("textbox", { name: "指示" })).not.toBeInTheDocument();
});

test("conflict reconciliation treats a stopped worker with a remaining active phase as idle", async () => {
  const socket = await mount();
  act(() => { socket.open(); socket.message(snapshot()); });
  const stopped = parseTaskStatus({ ...waiting, revision: 4, state: "executing", is_running: false,
    manual_input: null }, "desktop");
  vi.mocked(controlTask).mockRejectedValueOnce(new ApiError(409, "実行は終了しています"));
  vi.mocked(getCurrentTask).mockResolvedValueOnce(stopped);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "停止" })); });
  expect(screen.queryByText(waiting.manual_input!.reason)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "一時停止" })).toBeDisabled();
  expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled();
  expect(viewers[0].viewOnly).toBe(true);
  act(() => { socket.message({ type: "state", server_id: "server-1", vm_id: "desktop", session_id: "running",
    session_order: 1, revision: 5, state: "executing", action_count: 1, subtask_index: 0, subtask_count: 0 }); });
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  expect(viewers[0].viewOnly).toBe(true);
});

test("a valid legacy restoration without an environment never enables unbound controls or input", async () => {
  vi.mocked(getCurrentTask).mockResolvedValueOnce(parseTaskStatus({ ...waiting, vm_id: undefined,
    state: "executing", manual_input: null }));
  vi.mocked(getVms).mockResolvedValue([]);
  await act(async () => { render(<Home />); });
  expect(Socket.instances).toHaveLength(1);
  expect(viewers).toHaveLength(0);
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  expect(screen.getByRole("textbox", { name: "指示" })).toBeDisabled();
  expect(controlTask).not.toHaveBeenCalled();
});

test("a legacy state event updates selected progress without authorizing manual input", async () => {
  const socket = await mount();
  act(() => {
    socket.open(); socket.message(snapshot([]));
    socket.message({ type: "state", server_id: "server-1", session_id: "legacy", vm_id: null,
      session_order: 2, state: "executing", action_count: 0, subtask_index: 0, subtask_count: 1,
      subtasks: [{ id: "report", description: "報告の確認を進める" }] });
  });
  expect(screen.getByText("報告の確認を進める")).toBeVisible();
  expect(viewers[0].viewOnly).toBe(true);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "停止" })); });
  expect(controlTask).toHaveBeenCalledExactlyOnceWith("stop", "desktop", "legacy", undefined);
});

test("a lost status channel revokes input until a fresh snapshot, while stop remains available", async () => {
  const first = await mount();
  act(() => { first.open(); first.message(snapshot()); });
  expect(viewers[0].viewOnly).toBe(false);
  act(() => first.close());
  expect(viewers[0].viewOnly).toBe(true);
  expect(screen.getByRole("button", { name: "画面を送信して再開" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  await act(() => vi.advanceTimersByTimeAsync(3000));
  const second = Socket.instances[1];
  act(() => { second.open(); second.message({ type: "pong" }); });
  expect(viewers[0].viewOnly).toBe(true);
  act(() => first.message(snapshot()));
  expect(viewers[0].viewOnly).toBe(true);
  act(() => second.message(snapshot()));
  expect(viewers[0].viewOnly).toBe(false);
  expect(screen.getByRole("button", { name: "画面を送信して再開" })).toBeEnabled();
  act(() => { second.close(); });
  await act(() => vi.advanceTimersByTimeAsync(3000));
  const third = Socket.instances[2];
  act(() => { third.open(); third.message(snapshot([{ ...waiting, revision: 4, state: "executing", manual_input: null }])); });
  expect(viewers[0].viewOnly).toBe(true);
  expect(screen.queryByText(waiting.manual_input!.reason)).not.toBeInTheDocument();
});

test("an unresponsive status channel revokes input at the heartbeat deadline without waiting for close", async () => {
  const socket = await mount();
  act(() => { socket.open(); socket.message(snapshot()); });
  socket.close.mockImplementation(() => { socket.readyState = 2; });
  await act(() => vi.advanceTimersByTimeAsync(30000));
  expect(socket.close).toHaveBeenCalledOnce();
  expect(viewers[0].viewOnly).toBe(true);
  act(() => socket.message(snapshot()));
  expect(viewers[0].viewOnly).toBe(true);
  await act(() => vi.advanceTimersByTimeAsync(3000));
  expect(Socket.instances).toHaveLength(2);
  act(() => { Socket.instances[1].open(); Socket.instances[1].message(snapshot()); });
  expect(viewers[0].viewOnly).toBe(false);
  act(() => socket.onclose?.());
  expect(viewers[0].viewOnly).toBe(false);
});

test("a new server learned over HTTP cannot inherit manual input authorization from the old status channel", async () => {
  const socket = await mount();
  act(() => { socket.open(); socket.message(snapshot()); });
  expect(viewers[0].viewOnly).toBe(false);
  vi.mocked(controlTask).mockRejectedValueOnce(new ApiError(409, "対象が変わりました"));
  vi.mocked(getCurrentTask).mockResolvedValueOnce({ ...waiting, server_id: "replacement" });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "停止" })));
  expect(getCurrentTask).toHaveBeenCalledWith("desktop");
  expect(viewers[0].viewOnly).toBe(true);
  act(() => socket.message(snapshot()));
  expect(viewers[0].viewOnly).toBe(true);
  act(() => socket.message({ ...snapshot([{ ...waiting, server_id: "replacement" }]), server_id: "replacement" }));
  expect(viewers[0].viewOnly).toBe(false);
});

test.each([
  ["null", null],
  ["primitive", "state"],
  ["invalid snapshot", { type: "snapshot", tasks: [null] }],
  ["invalid running flag", { type: "snapshot", tasks: [{ ...waiting, is_running: "false" }] }],
  ["invalid completion", { type: "complete", success: "false", vm_id: "desktop", session_id: "running" }],
  ["invalid identity", { type: "state", state: "awaiting_user", vm_id: "desktop", session_id: "running",
    subtask_index: 0, subtask_count: 0, action_count: 0, manual_input: { id: [], reason: "broken" } }],
  ["duplicate environment", { type: "snapshot", tasks: [waiting, { ...waiting, session_id: "ambiguous" }] }],
])("a malformed %s revokes input without replacing the readable task and reconnects safely", async (_name, value) => {
  const first = await mount();
  act(() => { first.open(); first.message(snapshot()); });
  expect(viewers[0].viewOnly).toBe(false);
  expect(() => act(() => first.message(value))).not.toThrow();
  expect(viewers[0].viewOnly).toBe(true);
  expect(screen.getByText(waiting.manual_input!.reason)).toBeVisible();
  expect(screen.getByRole("button", { name: "画面を送信して再開" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  await act(() => vi.advanceTimersByTimeAsync(3000));
  const second = Socket.instances[1];
  act(() => { second.open(); second.message({ type: "pong" }); });
  expect(viewers[0].viewOnly).toBe(true);
  act(() => first.message(snapshot()));
  expect(viewers[0].viewOnly).toBe(true);
  act(() => second.message(snapshot()));
  expect(viewers[0].viewOnly).toBe(false);
});

test("a snapshot is validated atomically before it can authorize restored manual input", async () => {
  const socket = await mount();
  act(() => socket.open());
  expect(viewers[0].viewOnly).toBe(true);
  expect(() => act(() => socket.message({ ...snapshot(), tasks: [waiting, null] }))).not.toThrow();
  expect(viewers[0].viewOnly).toBe(true);
  expect(screen.getByRole("button", { name: "画面を送信して再開" })).toBeDisabled();
});
