import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import Home from "./page";
import { createVm, deleteVm, getCurrentTask, getTaskHistory, getVms, startVm, stopVm } from "@/lib/api";
import type { VmInfo } from "@/lib/types";

vi.mock("@/components/VncViewer", () => ({ default: () => <div data-testid="viewer" /> }));
vi.mock("@/components/VMControls", () => ({ default: () => null }));
vi.mock("@/hooks/useWebSocket", () => ({ useWebSocket: () => undefined }));
vi.mock("@/lib/api", () => ({
  ApiError: class extends Error {},
  getAuthStatus: async () => ({ authenticated: true }),
  getVms: vi.fn(), getCurrentTask: vi.fn(), getTaskHistory: vi.fn(),
  createVm: vi.fn(), deleteVm: vi.fn(), stopVm: vi.fn(), startVm: vi.fn(),
  getTaskDetail: vi.fn(), controlTask: vi.fn(), createTask: vi.fn(), deleteTask: vi.fn(),
  getVncWsUrl: (id: string) => `ws://localhost/vnc/${id}`,
}));

const desktop: VmInfo = { id: "desktop", name: "作業環境", status: "running", health: "healthy",
  managed: false, kind: "container", vnc_host: "desktop", vnc_port: 5900, ws_port: 0 };
let vms: VmInfo[];

beforeEach(() => {
  vi.resetAllMocks();
  sessionStorage.clear();
  vms = [{ ...desktop }];
  vi.mocked(getVms).mockImplementation(async () => vms);
  vi.mocked(getTaskHistory).mockResolvedValue([]);
  vi.mocked(getCurrentTask).mockResolvedValue({ session_id: null, vm_id: "desktop", state: "idle",
    is_running: false, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0 });
});
afterEach(cleanup);

async function openWorkspace() {
  render(<Home />);
  await screen.findByRole("button", { name: "作業環境を停止" });
  await waitFor(() => expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled());
}

async function submitCreation(label: string, name: string) {
  fireEvent.click(screen.getByRole("button", { name: `+ ${label}追加` }));
  fireEvent.change(screen.getByRole("textbox", { name: `${label}名` }), { target: { value: name } });
  fireEvent.click(screen.getByRole("button", { name: "作成する" }));
}

test.each([
  ["コンテナ", "container"], ["VM", "qemu"], ["Android", "android"],
] as const)("creating %s selects its own environment and refreshes readiness", async (label, kind) => {
  const created = { ...desktop, id: "new-env", name: "新しい環境", kind, managed: true };
  vi.mocked(createVm).mockImplementation(async () => { vms = [...vms, created]; return created; });
  await openWorkspace();
  await submitCreation(label, "新しい環境");
  await waitFor(() => expect(createVm).toHaveBeenCalledWith("新しい環境", kind, expect.any(AbortSignal)));
  const tab = await screen.findByText("新しい環境", { selector: ".vm-tab-name" });
  await waitFor(() => expect(tab.closest(".vm-tab")).toHaveClass("selected"));
  expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled();
  expect(screen.getByText(`${label}作成開始: 新しい環境`)).toBeVisible();
});

test.each([new Error("空き容量が不足しています"), "空き容量が不足しています"])(
  "creation failures preserve the current environment and allow retry: %s", async (error) => {
    vi.mocked(createVm).mockRejectedValueOnce(error);
    await openWorkspace();
    await submitCreation("コンテナ", "");
    expect(await screen.findByText("コンテナ作成失敗: 空き容量が不足しています")).toBeVisible();
    expect(createVm).toHaveBeenCalledWith(undefined, "container", expect.any(AbortSignal));
    expect(screen.getByText("作業環境", { selector: ".vm-tab-name" }).closest(".vm-tab")).toHaveClass("selected");
    expect(screen.getByRole("button", { name: "+ コンテナ追加" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled();
  });

test("creation completing after navigation does not move the user back", async () => {
  const other = { ...desktop, id: "other", name: "別の環境" };
  vms.push(other);
  let finish!: (value: VmInfo) => void;
  vi.mocked(createVm).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  await openWorkspace();
  await submitCreation("コンテナ", "作成待ち");
  fireEvent.click(screen.getByText("別の環境", { selector: ".vm-tab-name" }).closest("button")!);
  const created = { ...desktop, id: "new-env", name: "作成待ち" };
  vms = [...vms, created];
  await act(async () => finish(created));
  await screen.findByText("作成待ち", { selector: ".vm-tab-name" });
  expect(screen.getByText("別の環境", { selector: ".vm-tab-name" }).closest(".vm-tab")).toHaveClass("selected");
});

test("stopping and starting an environment updates both viewer and task admission", async () => {
  vi.mocked(stopVm).mockImplementation(async () => { vms = [{ ...desktop, status: "exited" }]; return vms[0]; });
  vi.mocked(startVm).mockImplementation(async () => { vms = [{ ...desktop }]; return vms[0]; });
  await openWorkspace();
  fireEvent.click(screen.getByRole("button", { name: "作業環境を停止" }));
  fireEvent.click(screen.getByRole("button", { name: "作業環境の停止を実行" }));
  await screen.findByRole("button", { name: "作業環境を起動" });
  expect(stopVm).toHaveBeenCalledWith("desktop");
  expect(screen.queryByTestId("viewer")).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "指示" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "作業環境を起動" }));
  await screen.findByRole("button", { name: "作業環境を停止" });
  expect(startVm).toHaveBeenCalledWith("desktop");
  expect(screen.getByTestId("viewer")).toBeVisible();
  expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled();
  expect(screen.getByText("停止: desktop（実行中タスクは停止）")).toBeVisible();
  expect(screen.getByText("起動: desktop")).toBeVisible();
});

test("deleting the selected environment switches to a surviving environment", async () => {
  vms.push({ ...desktop, id: "other", name: "残す環境" });
  vi.mocked(deleteVm).mockImplementation(async () => { vms = vms.filter(v => v.id !== "desktop"); return { status: "deleted" }; });
  await openWorkspace();
  fireEvent.click(screen.getByRole("button", { name: "作業環境を削除" }));
  fireEvent.click(screen.getByRole("button", { name: "作業環境の削除を実行" }));
  await waitFor(() => expect(screen.queryByText("作業環境", { selector: ".vm-tab-name" })).not.toBeInTheDocument());
  expect(deleteVm).toHaveBeenCalledWith("desktop");
  expect(screen.getByText("残す環境", { selector: ".vm-tab-name" }).closest(".vm-tab")).toHaveClass("selected");
  expect(screen.getByRole("textbox", { name: "指示" })).toBeEnabled();
});

test.each(["delete", "stop", "start"] as const)("a successful %s arriving after unmount cannot refresh the abandoned workspace", async operation => {
  const method = { delete: deleteVm, stop: stopVm, start: startVm }[operation];
  const verb = { delete: "削除", stop: "停止", start: "起動" }[operation];
  if (operation === "start") vms = [{ ...desktop, status: "exited" }];
  let finish!: () => void;
  const pending = new Promise<void>(resolve => { finish = resolve; });
  vi.mocked(deleteVm).mockImplementationOnce(async () => { await pending; return { status: "deleted" }; });
  vi.mocked(stopVm).mockImplementationOnce(async () => { await pending; return { ...desktop, status: "exited" }; });
  vi.mocked(startVm).mockImplementationOnce(async () => { await pending; return desktop; });
  const workspace = render(<Home />);
  try {
    fireEvent.click(await screen.findByRole("button", { name: `作業環境を${verb}` }));
    if (operation !== "start") fireEvent.click(screen.getByRole("button", { name: `作業環境の${verb}を実行` }));
    await waitFor(() => expect(method).toHaveBeenCalledExactlyOnceWith("desktop"));
    workspace.unmount();
    const reads = vi.mocked(getVms).mock.calls.length;
    await act(async () => { finish(); await pending; });
    expect(getVms).toHaveBeenCalledTimes(reads);
    expect(method).toHaveBeenCalledOnce();
    expect(screen.queryByRole("textbox", { name: "指示" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("viewer")).not.toBeInTheDocument();
  } finally {
    finish();
    await act(async () => { await pending; });
  }
});

test.each(["delete", "stop", "start"] as const)("failed %s keeps the existing VM state and reports the cause", async operation => {
  const method = { delete: deleteVm, stop: stopVm, start: startVm }[operation];
  const verb = { delete: "削除", stop: "停止", start: "起動" }[operation];
  if (operation === "start") vms = [{ ...desktop, status: "exited" }];
  vi.mocked(method).mockRejectedValueOnce(new Error("Dockerとの接続が切れました"));
  render(<Home />);
  const button = await screen.findByRole("button", { name: `作業環境を${verb}` });
  fireEvent.click(button);
  if (operation !== "start") fireEvent.click(screen.getByRole("button", { name: `作業環境の${verb}を実行` }));
  expect(await screen.findByText(`${verb}失敗: Dockerとの接続が切れました`)).toBeVisible();
  expect(method).toHaveBeenCalledWith("desktop");
  expect(screen.getByRole("button", { name: `作業環境を${operation === "start" ? "起動" : "停止"}` })).toBeEnabled();
});

test.each(["delete", "stop", "start"] as const)("a non-Error %s failure preserves the draft and supports an explicit retry", async operation => {
  const method = { delete: deleteVm, stop: stopVm, start: startVm }[operation];
  const verb = { delete: "削除", stop: "停止", start: "起動" }[operation];
  if (operation === "start") vms = [{ ...desktop, status: "exited" }];
  vi.mocked(method).mockRejectedValueOnce("Dockerとの接続が切れました");
  vi.mocked(deleteVm).mockImplementation(async () => { vms = []; return { status: "deleted" }; });
  vi.mocked(stopVm).mockImplementation(async () => { vms = [{ ...desktop, status: "exited" }]; return vms[0]; });
  vi.mocked(startVm).mockImplementation(async () => { vms = [{ ...desktop }]; return vms[0]; });
  render(<Home />);
  const button = await screen.findByRole("button", { name: `作業環境を${verb}` });
  const input = screen.getByRole("textbox", { name: "指示" });
  fireEvent.change(input, { target: { value: "編集中の報告の指示" } });
  fireEvent.click(button);
  if (operation !== "start") fireEvent.click(screen.getByRole("button", { name: `作業環境の${verb}を実行` }));
  expect(await screen.findByText(`${verb}失敗: Dockerとの接続が切れました`)).toBeVisible();
  expect(method).toHaveBeenCalledOnce();
  expect(input).toHaveValue("編集中の報告の指示");
  fireEvent.click(screen.getByRole("button", { name: `作業環境を${verb}` }));
  if (operation !== "start") fireEvent.click(screen.getByRole("button", { name: `作業環境の${verb}を実行` }));
  await waitFor(() => expect(method).toHaveBeenCalledTimes(2));
  expect(input).toHaveValue("編集中の報告の指示");
  if (operation === "delete") {
    await waitFor(() => expect(screen.queryByText(desktop.name, { selector: ".vm-tab-name" })).not.toBeInTheDocument());
    expect(input).toBeDisabled();
    expect(screen.queryByTestId("viewer")).not.toBeInTheDocument();
  } else {
    await screen.findByRole("button", { name: `作業環境を${operation === "start" ? "停止" : "起動"}` });
    if (operation === "start") expect(input).toBeEnabled();
    else expect(input).toBeDisabled();
  }
});
