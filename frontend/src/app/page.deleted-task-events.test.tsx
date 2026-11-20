import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { TaskStatus, WsMessage } from "@/lib/types";
import { parseWsMessage } from "@/lib/task-protocol";
import Home from "./page";

const ws = vi.hoisted(() => ({ message: null as null | ((value: WsMessage) => void) }));
vi.mock("@/hooks/useWebSocket", () => ({ useWebSocket: (message: typeof ws.message) => { ws.message = message; } }));
vi.mock("@/components/VncViewer", () => ({ default: ({ allowInput = false }: { allowInput?: boolean }) =>
  <div data-testid="viewer" data-input-enabled={String(allowInput)} /> }));
vi.mock("@/components/VMControls", () => ({ default: () => null }));

const vm = { id: "desktop", name: "作業環境", status: "running", health: "healthy", managed: false,
  kind: "container", vnc_host: "desktop", vnc_port: 5900, ws_port: 0 };
let serverId: string;
let current: TaskStatus;
let history: ReturnType<typeof summary>[];
let deletionGate: Promise<void> | null;
let deletionFails: boolean;
let postGate: Promise<void> | null;

function status(id: string, description = "売上報告を保存する"): TaskStatus {
  return { server_id: serverId, session_id: id, vm_id: vm.id, state: "executing", is_running: true,
    action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [{ id: "report", description }], current_subtask_index: 0 };
}

function summary(id: string, instruction = "売上報告を保存する") {
  return { id, vm_id: vm.id, instruction, state: "executing", success: null,
    action_count: 0, success_count: 0, failure_count: 0, updated_at: 1700000000 };
}

const response = (value: unknown, code = 200) => new Response(JSON.stringify(value), {
  status: code, headers: { "Content-Type": "application/json" },
});

beforeEach(() => {
  sessionStorage.clear(); localStorage.clear();
  serverId = "server-one";
  current = status("old-task");
  history = [summary("old-task")];
  deletionGate = null; deletionFails = false; postGate = null;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    const path = new URL(String(url)).pathname;
    if (path === "/auth/status") return response({ authenticated: true });
    if (path === "/vms") return response([vm]);
    if (path === "/tasks/current") return response(current);
    if (path === "/tasks/current/stop") return response({ status: "stopped" });
    if (path === "/tasks") {
      if (init?.method === "POST") {
        const instruction = JSON.parse(String(init.body)).instruction as string;
        const accepted = status("pending-task", instruction);
        current = accepted;
        history = [summary("pending-task", instruction)];
        if (postGate) await postGate;
        return response(accepted);
      }
      return response(history);
    }
    if (path.startsWith("/tasks/") && init?.method === "DELETE") {
      const origin = serverId;
      const id = decodeURIComponent(path.slice("/tasks/".length));
      const fails = deletionFails;
      if (deletionGate) await deletionGate;
      if (fails) return response({ detail: "旧サーバーの保存先を確認してください" }, 503);
      if (origin === serverId) history = history.filter(item => item.id !== id);
      return response({ status: "deleted" });
    }
    throw new Error(`unexpected request: ${path}`);
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

async function broadcast(value: unknown) {
  // Exercise the same JSON decoder and protocol validator as the real socket.
  await act(async () => ws.message?.(parseWsMessage(JSON.stringify(value))));
}

async function openAndDeleteCurrent() {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  await broadcast({ type: "snapshot", server_id: serverId, tasks: [current] });
  fireEvent.click(await screen.findByRole("button", { name: "売上報告を保存するを削除" }));
  await waitFor(() => expect(screen.queryByTitle("売上報告を保存する")).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled();
}

test.each(["state", "action", "error", "complete", "snapshot"] as const)(
  "a delayed legacy %s cannot restore a deleted task, while a new task stays controllable", async kind => {
    await openAndDeleteCurrent();
    const identity = { server_id: serverId, session_id: "old-task", vm_id: vm.id };
    const waiting = { ...status("old-task", "削除済みの進捗"), state: "awaiting_user",
      manual_input: { id: "retired-consent", reason: "削除済みの認証待ち" } };
    const packet = kind === "state"
      ? { type: kind, ...identity, state: "awaiting_user", subtask_index: 0, subtask_count: 1,
        action_count: 0, manual_input: waiting.manual_input }
      : kind === "action" ? { type: kind, ...identity, action_type: "type", description: "削除済みの操作", success: true }
        : kind === "error" ? { type: kind, ...identity, message: "削除済みのエラー" }
          : kind === "complete" ? { type: kind, ...identity, success: false }
            : { type: kind, server_id: serverId, tasks: [waiting] };
    await broadcast(packet);
    expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled();
    expect(screen.getByTestId("viewer")).toHaveAttribute("data-input-enabled", "false");
    expect(screen.queryByText(/削除済みの/)).not.toBeInTheDocument();
    expect(screen.queryByText("タスク失敗")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "画面を送信して再開" })).not.toBeInTheDocument();

    current = status("next-task", "次の報告を保存する");
    await broadcast({ type: "snapshot", server_id: serverId, tasks: [current] });
    expect(screen.getByText("次の報告を保存する", { selector: ".subtask-desc" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "停止" }));
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => {
      const target = new URL(String(url));
      return target.pathname === "/tasks/current/stop" && target.searchParams.get("session_id") === "next-task";
    })).toBe(true));
  },
);

test("consecutive deletions reject delayed events from both legacy task IDs", async () => {
  await openAndDeleteCurrent();
  current = status("second-task", "次の報告を保存する");
  history = [summary("second-task", "次の報告を保存する")];
  await broadcast({ type: "snapshot", server_id: serverId, tasks: [current] });
  fireEvent.click(await screen.findByRole("button", { name: "次の報告を保存するを削除" }));
  await waitFor(() => expect(screen.queryByTitle("次の報告を保存する")).not.toBeInTheDocument());
  for (const id of ["old-task", "second-task"]) {
    await broadcast({ type: "state", server_id: serverId, session_id: id, vm_id: vm.id,
      state: "executing", action_count: 0, subtask_index: 0, subtask_count: 1 });
  }
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled();
});

test("a new server generation can restore a task ID removed from the old server", async () => {
  await openAndDeleteCurrent();
  serverId = "server-two";
  current = status("old-task", "復元後の報告を保存する");
  history = [summary("old-task", "復元後の報告を保存する")];
  await broadcast({ type: "snapshot", server_id: serverId, tasks: [current] });
  expect(screen.getByText("復元後の報告を保存する", { selector: ".subtask-desc" })).toBeVisible();
  expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  expect(screen.getByRole("textbox", { name: "指示" })).toBeDisabled();
});

test("an empty environment snapshot remains usable after deletion", async () => {
  await openAndDeleteCurrent();
  await broadcast({ type: "snapshot", server_id: serverId, tasks: [{ ...current,
    session_id: null, state: "idle", is_running: false, subtasks: [] }] });
  expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled();
});

test("a delayed acceptance cannot restore a task deleted while its POST response was pending", async () => {
  current = { ...current, session_id: null, state: "idle", is_running: false, subtasks: [] };
  history = [];
  let finish!: () => void;
  postGate = new Promise(resolve => { finish = resolve; });
  render(<Home />);
  try {
    const input = await screen.findByRole("textbox", { name: "指示" });
    await waitFor(() => expect(input).toBeEnabled());
    fireEvent.change(input, { target: { value: "受付待ちの報告" } });
    fireEvent.click(screen.getByRole("button", { name: "実行" }));
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url, init]) =>
      new URL(String(url)).pathname === "/tasks" && init?.method === "POST")).toBe(true));
    await broadcast({ type: "snapshot", server_id: serverId, tasks: [current] });
    fireEvent.click(await screen.findByRole("button", { name: "受付待ちの報告を削除" }));
    await waitFor(() => expect(screen.queryByTitle("受付待ちの報告")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
    await act(async () => { finish(); await postGate; });
    await waitFor(() => expect(input).toHaveValue(""));
    expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
    expect(input).toBeEnabled();
    expect(screen.queryByText("受付待ちの報告", { selector: ".subtask-desc" })).not.toBeInTheDocument();
  } finally {
    finish();
    await act(async () => { await postGate; });
  }
});

test.each([false, true])("an old deletion acknowledgement cannot remove the new server's record (failure: %s)", async fails => {
  let finish!: () => void;
  deletionGate = new Promise(resolve => { finish = resolve; });
  deletionFails = fails;
  render(<Home />);
  try {
    const remove = await screen.findByRole("button", { name: "売上報告を保存するを削除" });
    fireEvent.click(remove);
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url, init]) =>
      new URL(String(url)).pathname === "/tasks/old-task" && init?.method === "DELETE")).toBe(true));
    serverId = "server-two";
    current = status("old-task", "新しいサーバーの報告を保存する");
    history = [summary("old-task", "新しいサーバーの報告を保存する")];
    await broadcast({ type: "snapshot", server_id: serverId, tasks: [current] });
    await screen.findByTitle("新しいサーバーの報告を保存する");
    await act(async () => { finish(); await deletionGate; });
    await waitFor(() => expect(screen.getByRole("button", { name: "新しいサーバーの報告を保存するを削除" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
    expect(screen.getByText("新しいサーバーの報告を保存する", { selector: ".subtask-desc" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  } finally {
    finish();
    await act(async () => { await deletionGate; });
  }
});
