import { afterEach, beforeEach, expect, test, vi } from "vitest";

const vm = { id: "desk-" + "a".repeat(24), name: "保存する環境", kind: "container", status: "creating",
  managed: true, vnc_host: "desktop", health: null, vnc_port: 0, ws_port: 0 };
const receipt = { id: "creation-" + "a".repeat(64), vm_id: vm.id, state: "succeeded",
  result: vm, error: null, error_status: null };
const response = (body: unknown, status = 200) => ({ ok: status === 200, status,
  json: async () => body }) as Response;

beforeEach(() => { vi.resetModules(); sessionStorage.clear(); localStorage.clear(); });
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

test.each([
  null, "receipt", { ...receipt, id: "different" }, { ...receipt, vm_id: 1 },
  { ...receipt, state: "unknown" }, { ...receipt, result: null },
  ...[
    { id: "another-environment" }, { kind: "android" }, { name: 1 },
    { status: null }, { managed: false }, { vnc_host: null },
    { health: false }, { vnc_port: -1 }, { ws_port: 65536 },
  ].map(changes => ({ ...receipt, result: { ...vm, ...changes } })),
  { ...receipt, state: "failed", result: null, error: null, error_status: 503 },
  { ...receipt, state: "failed", result: null, error: "作成を中断しました", error_status: 404 },
  { ...receipt, state: "failed", result: null, error: "作成を中断しました", error_status: null },
  { ...receipt, vm_id: "", result: { ...vm, id: "" } },
])("a malformed creation receipt cannot publish a VM or consume its retry identity: %j", async value => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response(value))
    .mockResolvedValueOnce(response(receipt));
  const { createVm } = await import("./api");
  const { pendingVmCreations } = await import("./vm-creation-retries");
  await expect(createVm(vm.name, "container")).rejects.toThrow("作成結果を確認できません");
  expect(pendingVmCreations()).toHaveLength(1);
  expect(await createVm(vm.name, "container")).toEqual(vm);
  expect(fetch.mock.calls[0][1]?.headers).toEqual(fetch.mock.calls[1][1]?.headers);
  expect(pendingVmCreations()).toEqual([]);
});

test.each([400, 409, 503])("a recorded terminal failure %i releases the receipt and preserves its cause", async status => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response({ ...receipt, result: null,
    state: "failed", error: "環境を作成できません", error_status: status }));
  const { createVm } = await import("./api");
  const { pendingVmCreations } = await import("./vm-creation-retries");
  await expect(createVm(vm.name, "container")).rejects.toMatchObject({ status, message: "環境を作成できません" });
  expect(pendingVmCreations()).toEqual([]);
});

test.each([["保存先", "unknown"], ["長".repeat(129), "container"]])("invalid creation settings fail before allocating a receipt: %s/%s", async (name, kind) => {
  const fetch = vi.spyOn(globalThis, "fetch");
  const { createVm } = await import("./api");
  const { pendingVmCreations } = await import("./vm-creation-retries");
  await expect(createVm(name, kind)).rejects.toMatchObject({ status: 400 });
  expect(fetch).not.toHaveBeenCalled();
  expect(pendingVmCreations()).toEqual([]);
});

test("an already aborted creation does not allocate a receipt or start a request", async () => {
  const fetch = vi.spyOn(globalThis, "fetch");
  const { createVm } = await import("./api");
  const { pendingVmCreations } = await import("./vm-creation-retries");
  const controller = new AbortController();
  controller.abort();
  await expect(createVm(vm.name, "container", controller.signal)).rejects.toMatchObject({ name: "AbortError" });
  expect(fetch).not.toHaveBeenCalled();
  expect(pendingVmCreations()).toEqual([]);
});

test("a poll response for a different receipt cannot complete the original request", async () => {
  vi.useFakeTimers();
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response({ ...receipt, state: "running", result: null }))
    .mockResolvedValueOnce(response({ ...receipt, id: "creation-" + "b".repeat(64) }));
  const { createVm } = await import("./api");
  const { pendingVmCreations } = await import("./vm-creation-retries");
  const failed = createVm(vm.name, "container").catch(error => error);
  await vi.advanceTimersByTimeAsync(500);
  expect(await failed).toMatchObject({ message: "作成結果を確認できません。同じ内容でもう一度操作してください" });
  expect(pendingVmCreations()).toHaveLength(1);
});

test("authentication confirmation without a caller guard still locks an expired session once", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response({ authenticated: false }));
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const { confirmAuthentication } = await import("./api");
  await confirmAuthentication();
  expect(dispatch).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ type: "auth-required" }));
});

test("an already aborted auth check never starts a status request", async () => {
  const fetch = vi.spyOn(globalThis, "fetch");
  const { getAuthStatus } = await import("./api");
  const controller = new AbortController();
  controller.abort();
  await expect(getAuthStatus(controller.signal)).rejects.toMatchObject({ name: "AbortError" });
  expect(fetch).not.toHaveBeenCalled();
});

test.each([null, {}, { authenticated: "false" }, { authenticated: 1 }, { authenticated: [] }])(
  "authentication requires an explicit boolean status: %j", async value => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response(value));
    const { getAuthStatus } = await import("./api");
    await expect(getAuthStatus()).rejects.toThrow("認証の応答が不正です");
  },
);
