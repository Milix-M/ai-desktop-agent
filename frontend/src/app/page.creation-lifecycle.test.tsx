import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import Home from "./page";
import { createVm, getAuthStatus, getCurrentTask, getTaskHistory, getVms } from "@/lib/api";
import { pendingVmCreations, vmCreationRetry } from "@/lib/vm-creation-retries";
import type { VmInfo } from "@/lib/types";

vi.mock("@/components/VncViewer", () => ({ default: () => <div /> }));
vi.mock("@/components/VMControls", () => ({ default: () => null }));
vi.mock("@/components/ConnectionPanel", () => ({ default: () => null }));
vi.mock("@/hooks/useWebSocket", () => ({ useWebSocket: () => undefined }));
vi.mock("@/lib/api", async load => ({
  ...await load<typeof import("@/lib/api")>(),
  getAuthStatus: vi.fn(), getVms: vi.fn(), getCurrentTask: vi.fn(), getTaskHistory: vi.fn(), createVm: vi.fn(),
}));

const native = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
const desktop: VmInfo = { id: "desktop", name: "作業環境", status: "running", health: "healthy",
  managed: false, kind: "container", vnc_host: "desktop", vnc_port: 5900, ws_port: 0 };
const vm = { ...desktop, id: "desk-" + "a".repeat(24), managed: true, name: "作成中の環境" };
const queued = { id: "creation-" + "a".repeat(64), vm_id: vm.id, state: "queued",
  result: null, error: null, error_status: null };
const response = (value: unknown) => ({ ok: true, status: 200, json: async () => value }) as Response;

beforeEach(() => {
  vi.resetAllMocks();
  sessionStorage.clear();
  vi.mocked(createVm).mockImplementation(native.createVm);
  vi.mocked(getAuthStatus).mockResolvedValue({ authenticated: true });
  vi.mocked(getVms).mockResolvedValue([desktop]);
  vi.mocked(getTaskHistory).mockResolvedValue([]);
  vi.mocked(getCurrentTask).mockResolvedValue({ session_id: null, vm_id: "desktop", state: "idle",
    is_running: false, action_count: 0, success_count: 0, failure_count: 0,
    subtasks: [], current_subtask_index: 0 });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

test("a transport observer reentering the actual form cannot create another environment", async () => {
  let reentered = false;
  const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    expect(String(url)).toMatch(/\/vm-creations$/);
    expect(init?.method).toBe("POST");
    if (!reentered) {
      reentered = true;
      const button = screen.getByRole("button", { name: "作成する" });
      expect(button).toBeEnabled();
      button.click();
    }
    return response({ ...queued, state: "succeeded", result: vm });
  });
  render(<Home />);
  fireEvent.click(await screen.findByRole("button", { name: "+ コンテナ追加" }));
  fireEvent.change(screen.getByRole("textbox", { name: "コンテナ名" }), { target: { value: vm.name } });
  fireEvent.click(screen.getByRole("button", { name: "作成する" }));
  expect(await screen.findByText(`コンテナ作成開始: ${vm.name}`)).toBeVisible();
  expect(reentered).toBe(true);
  expect(createVm).toHaveBeenCalledOnce();
  expect(fetch).toHaveBeenCalledOnce();
  expect(pendingVmCreations()).toEqual([]);
});

test("unmounting stops local creation polling while keeping the server receipt for the next login", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(response(queued));
  const workspace = render(<Home />);
  await screen.findByRole("button", { name: "+ コンテナ追加" });
  fireEvent.click(screen.getByRole("button", { name: "+ コンテナ追加" }));
  fireEvent.change(screen.getByRole("textbox", { name: "コンテナ名" }), { target: { value: vm.name } });
  fireEvent.click(screen.getByRole("button", { name: "作成する" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledOnce());
  const creation = vi.mocked(createVm).mock.results[0].value as Promise<VmInfo>;
  const receipt = pendingVmCreations();
  const reads = vi.mocked(getVms).mock.calls.length;
  workspace.unmount();
  try {
    await new Promise(resolve => setTimeout(resolve, 650));
    expect(fetch).toHaveBeenCalledOnce();
    expect(pendingVmCreations()).toEqual(receipt);
    expect(getVms).toHaveBeenCalledTimes(reads);
    expect(vi.mocked(createVm).mock.calls[0][2]?.aborted).toBe(true);
  } finally {
    fetch.mockResolvedValue(response({ ...queued, state: "succeeded", result: vm }));
    await act(async () => { await creation.catch(() => {}); });
  }
});

test.each([
  ["new", "succeeded"], ["new", "failed"],
  ["restored", "succeeded"], ["restored", "failed"],
] as const)("a %s creation completed as %s cannot refresh a workspace closed by an earlier response observer", async (source, state) => {
  let finishBody!: (value: unknown) => void;
  const body = new Promise<unknown>(resolve => { finishBody = resolve; });
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, status: 200,
    json: () => body } as Response);
  const close = () => workspace.unmount();
  vi.mocked(createVm).mockImplementation((...args) => {
    const completion = native.createVm(...args);
    // An independent observer can close the workspace in the microtask before
    // the component resumes from its await of this same public API promise.
    void completion.then(close, close);
    return completion;
  });
  const terminal = state === "succeeded"
    ? { ...queued, state, result: vm }
    : { ...queued, state, error: "環境を作成できません", error_status: 503 };
  if (source === "restored") vmCreationRetry(vm.name, "container");
  const workspace = render(<Home />);
  try {
    if (source === "new") {
      fireEvent.click(await screen.findByRole("button", { name: "+ コンテナ追加" }));
      fireEvent.change(screen.getByRole("textbox", { name: "コンテナ名" }), { target: { value: vm.name } });
      fireEvent.click(screen.getByRole("button", { name: "作成する" }));
    }
    await waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    expect(createVm).toHaveBeenCalledOnce();
    const reads = [getVms, getTaskHistory].map(fn => vi.mocked(fn).mock.calls.length);
    const completion = vi.mocked(createVm).mock.results[0].value as Promise<VmInfo>;
    await act(async () => { finishBody(terminal); await completion.catch(() => {}); });
    expect(vi.mocked(createVm).mock.calls[0][2]?.aborted).toBe(true);
    expect([getVms, getTaskHistory].map(fn => vi.mocked(fn).mock.calls.length)).toEqual(reads);
    expect(fetch).toHaveBeenCalledOnce();
    expect(pendingVmCreations()).toEqual([]);
    expect(screen.queryByRole("textbox", { name: "指示" })).not.toBeInTheDocument();
    expect(screen.queryByText(vm.name, { selector: ".vm-tab-name" })).not.toBeInTheDocument();
  } finally {
    finishBody(terminal);
    workspace.unmount();
    const completion = vi.mocked(createVm).mock.results[0]?.value as Promise<VmInfo> | undefined;
    await act(async () => { await completion?.catch(() => {}); });
  }
});
