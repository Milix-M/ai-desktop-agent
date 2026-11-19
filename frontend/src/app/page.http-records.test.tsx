import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { flushSync } from "react-dom";
import type { TaskDetail, WsMessage } from "@/lib/types";
import Home from "./page";

const ws = vi.hoisted(() => ({ message: null as null | ((value: WsMessage) => void) }));
vi.mock("@/hooks/useWebSocket", () => ({ useWebSocket: (message: typeof ws.message) => { ws.message = message; } }));
vi.mock("@/components/VncViewer", () => ({ default: () => <div data-testid="viewer" /> }));
vi.mock("@/components/VMControls", () => ({ default: () => null }));
vi.mock("@/components/ConnectionPanel", () => ({ default: () => null }));

const vm = { id: "desktop", name: "作業環境", status: "running", health: null, managed: false,
  kind: "container", vnc_host: "desktop", vnc_port: 5900, ws_port: 0 };
const saved: TaskDetail = { id: "saved-a", instruction: "保存済みの売上報告", state: "completed", success: true,
  action_count: 0, success_count: 0, failure_count: 0, updated_at: 1700000000, vm_id: "desktop",
  subtasks: [], actions: [], goal: {}, created_at: 1699999990, current_subtask_index: 0 };
const other: TaskDetail = { ...saved, id: "saved-b", instruction: "別の保存済み報告" };
let environments: unknown;
let history: unknown;
let secondDetail: unknown;
let secondMissing: boolean;
let secondRequest: Promise<unknown> | null;
let detailFailure: unknown;
let deleteResponse: unknown;
let deleteStalled: boolean;
const response = (value: unknown) => ({ ok: true, status: 200, json: async () => value }) as Response;

beforeEach(() => {
  vi.useFakeTimers(); sessionStorage.clear(); localStorage.clear();
  environments = [vm]; history = [saved, other]; secondDetail = other; secondMissing = false; secondRequest = null; detailFailure = null;
  deleteResponse = { status: "deleted" };
  deleteStalled = false;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    const path = new URL(String(url)).pathname;
    if (path === "/auth/status") return response({ authenticated: true });
    if (path === "/vms") return response(environments);
    if (path === "/tasks/current") return response({ server_id: "server", session_id: null, state: "idle",
      is_running: false, action_count: 0, success_count: 0, failure_count: 0, subtasks: [], current_subtask_index: 0 });
    if (path === "/tasks") return response(history);
    if (path === "/tasks/saved-a") return response(saved);
    if (path === "/tasks/saved-b") {
      if (init?.method === "DELETE") {
        if (deleteStalled) return new Promise((_, reject) => init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true }));
        return response(deleteResponse);
      }
      if (detailFailure !== null) throw detailFailure;
      if (secondMissing) return { ok: false, status: 404, json: async () => ({ detail: "履歴が削除されています" }) } as Response;
      return response(secondRequest ? await secondRequest : secondDetail);
    }
    throw new Error(`unexpected request: ${path}`);
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

async function openSavedTask() {
  await act(async () => { render(<Home />); });
  await act(async () => { fireEvent.click(screen.getByTitle(saved.instruction)); });
  expect(screen.getByText(`タスク: ${saved.instruction}（完了）`)).toBeVisible();
  fireEvent.change(screen.getByRole("textbox", { name: "指示" }), { target: { value: "編集中の指示を残す" } });
}

test("a transport subscriber locking the workspace during server refresh cannot request another detail", async () => {
  await openSavedTask();
  const fetch = vi.mocked(globalThis.fetch);
  const original = fetch.getMockImplementation()!;
  const detailRequests = () => fetch.mock.calls.filter(([url]) => new URL(String(url)).pathname === "/tasks/saved-a").length;
  const before = detailRequests();
  let locked = false;
  fetch.mockImplementation((url, init) => {
    if (!locked && new URL(String(url)).pathname === "/tasks") {
      locked = true;
      // A public transport observer can synchronously require authentication.
      // Preserve the real AuthGate, workspace lifecycle and API parser.
      flushSync(() => window.dispatchEvent(new Event("auth-required")));
    }
    return original(url, init);
  });
  await act(async () => { ws.message?.({ type: "snapshot", server_id: "server-two", tasks: [] }); });
  expect(locked).toBe(true);
  expect(screen.getByRole("button", { name: "ログイン" })).toBeVisible();
  expect(screen.queryByRole("textbox", { name: "指示" })).not.toBeInTheDocument();
  expect(detailRequests()).toBe(before);
});

test.each([null, [vm, { ...vm }], [vm, { ...vm, id: "other", name: {} }]].map(value => [value] as const))(
  "a failed environment/history refresh retains the selected task and draft: %j", async value => {
    await openSavedTask();
    environments = value;
    history = [saved, null];
    await act(async () => { ws.message?.({ type: "snapshot", server_id: "server", tasks: [] }); });
    await act(() => vi.advanceTimersByTimeAsync(15000));
    expect(screen.getByText(vm.name, { selector: ".vm-tab-name" }).closest(".vm-tab")).toHaveClass("selected");
    expect(screen.getByTitle(saved.instruction).closest(".history-item")).toHaveClass("selected");
    expect(screen.getByTitle(other.instruction)).toBeVisible();
    expect(screen.getByRole("textbox", { name: "指示" })).toHaveValue("編集中の指示を残す");
    expect(screen.getByText(`タスク: ${saved.instruction}（完了）`)).toBeVisible();
  },
);

test.each([null, { ...other, id: "different" }, { ...other, actions: null }])(
  "a rejected detail stays beside its record, preserves the previous task and can be retried: %j", async value => {
    await openSavedTask();
    secondDetail = value;
    await act(async () => { fireEvent.click(screen.getByTitle(other.instruction)); });
    expect(screen.getByText(`タスク: ${saved.instruction}（完了）`)).toBeVisible();
    expect(screen.getByTitle(saved.instruction).closest(".history-item")).toHaveClass("selected");
    expect(screen.getByRole("alert")).toHaveTextContent("タスク詳細の応答が不正です");
    expect(screen.getByRole("alert").parentElement).toContainElement(screen.getByTitle(other.instruction));
    expect(screen.getByRole("alert").parentElement).not.toContainElement(screen.getByTitle(saved.instruction));
    expect(screen.getByRole("textbox", { name: "指示" })).toHaveValue("編集中の指示を残す");
    secondDetail = other;
    await act(async () => { fireEvent.click(screen.getByTitle(other.instruction)); });
    expect(screen.getByText(`タスク: ${other.instruction}（完了）`)).toBeVisible();
    expect(screen.getByTitle(other.instruction).closest(".history-item")).toHaveClass("selected");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  },
);

test("a failed detail response arriving after a newer selection cannot publish an old error", async () => {
  await openSavedTask();
  let finish!: (value: unknown) => void;
  secondRequest = new Promise(resolve => { finish = resolve; });
  await act(async () => { fireEvent.click(screen.getByTitle(other.instruction)); });
  await act(async () => { fireEvent.click(screen.getByTitle(saved.instruction)); });
  await act(async () => finish(null));
  expect(screen.getByText(`タスク: ${saved.instruction}（完了）`)).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("opening a deleted record preserves the selected report and draft, and a restored record can be retried", async () => {
  await openSavedTask();
  secondMissing = true;
  await act(async () => { fireEvent.click(screen.getByTitle(other.instruction)); });
  expect(screen.getByText(`タスク: ${saved.instruction}（完了）`)).toBeVisible();
  expect(screen.getByTitle(saved.instruction).closest(".history-item")).toHaveClass("selected");
  expect(screen.getByRole("alert")).toHaveTextContent("履歴が削除されています (HTTP 404)");
  expect(screen.getByRole("alert").parentElement).toContainElement(screen.getByTitle(other.instruction));
  expect(screen.getByRole("textbox", { name: "指示" })).toHaveValue("編集中の指示を残す");
  secondMissing = false;
  await act(async () => { fireEvent.click(screen.getByTitle(other.instruction)); });
  expect(screen.getByText(`タスク: ${other.instruction}（完了）`)).toBeVisible();
  expect(screen.getByTitle(other.instruction).closest(".history-item")).toHaveClass("selected");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "指示" })).toHaveValue("編集中の指示を残す");
});

test("an unreadable record can still be deleted without clearing the currently selected task", async () => {
  await openSavedTask();
  secondDetail = null;
  await act(async () => { fireEvent.click(screen.getByTitle(other.instruction)); });
  expect(screen.getByRole("alert")).toBeVisible();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: `${other.instruction}を削除` })); });
  expect(screen.queryByTitle(other.instruction)).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByText(`タスク: ${saved.instruction}（完了）`)).toBeVisible();
});

test("a transport rejection without an Error object remains visible beside its record", async () => {
  await openSavedTask();
  detailFailure = "通信が途切れました";
  await act(async () => { fireEvent.click(screen.getByTitle(other.instruction)); });
  expect(screen.getByRole("alert")).toHaveTextContent("通信が途切れました");
  expect(screen.getByText(`タスク: ${saved.instruction}（完了）`)).toBeVisible();
});

test.each(["invalid acknowledgement", "timeout"])("a deletion %s keeps the selected detail until a confirmed retry", async failure => {
  await openSavedTask();
  await act(async () => { fireEvent.click(screen.getByTitle(other.instruction)); });
  deleteResponse = null;
  deleteStalled = failure === "timeout";
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: `${other.instruction}を削除` })); });
  if (deleteStalled) await act(() => vi.advanceTimersByTimeAsync(30000));
  expect(screen.getByRole("alert")).toHaveTextContent(deleteStalled ? "応答を確認できません" : "操作の応答が不正です");
  expect(screen.getByRole("button", { name: `${other.instruction}を削除` })).toBeEnabled();
  expect(vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
  expect(screen.getByTitle(other.instruction).closest(".history-item")).toHaveClass("selected");
  expect(screen.getByText(`タスク: ${other.instruction}（完了）`)).toBeVisible();
  expect(screen.getByRole("textbox", { name: "指示" })).toHaveValue("編集中の指示を残す");
  deleteResponse = { status: "deleted" };
  deleteStalled = false;
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: `${other.instruction}を削除` })); });
  expect(screen.queryByTitle(other.instruction)).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "指示" })).toHaveValue("編集中の指示を残す");
});
