import { afterEach, beforeEach, expect, test, vi } from "vitest";

const vm = { id: "desk-" + "a".repeat(24), name: "作業環境", kind: "container", status: "creating",
  health: null, managed: true, vnc_host: "desktop", vnc_port: 0, ws_port: 0 };
const succeeded = { id: "creation-" + "a".repeat(64), vm_id: vm.id, state: "succeeded",
  result: vm, error: null, error_status: null };
const response = (value: unknown, status = 200) => ({ ok: status === 200, status,
  json: async () => value }) as Response;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => { vi.resetModules(); sessionStorage.clear(); });
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

test.each(["accepted", "http-error"])("a cancelled creation ignores a late %s response and retains its receipt", async outcome => {
  const pending = deferred<Response>();
  const fetch = vi.spyOn(globalThis, "fetch").mockReturnValueOnce(pending.promise);
  const { createVm } = await import("./api");
  const { pendingVmCreations } = await import("./vm-creation-retries");
  const controller = new AbortController();
  const creation = createVm(vm.name, "container", controller.signal);
  const check = expect(creation).rejects.toMatchObject({ name: "AbortError" });
  const receipt = pendingVmCreations();
  controller.abort();
  pending.resolve(outcome === "accepted" ? response(succeeded) : response({ detail: "late error" }, 503));
  await check;
  expect(pendingVmCreations()).toEqual(receipt);
  expect(fetch).toHaveBeenCalledOnce();
});

test("an acknowledgement arriving after the request deadline cannot clear the retry receipt", async () => {
  vi.useFakeTimers();
  const pending = deferred<Response>();
  const fetch = vi.spyOn(globalThis, "fetch").mockReturnValueOnce(pending.promise);
  const { createVm } = await import("./api");
  const { pendingVmCreations } = await import("./vm-creation-retries");
  const creation = createVm(vm.name, "container");
  const check = expect(creation).rejects.toThrow("作成結果を確認できません");
  void check.catch(() => {});
  const receipt = pendingVmCreations();
  await vi.advanceTimersByTimeAsync(10000);
  expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(true);
  pending.resolve(response(succeeded));
  await check;
  expect(pendingVmCreations()).toEqual(receipt);
  expect(vi.getTimerCount()).toBe(0);
});

test("cancelling while the response body is being read cannot publish a creation result", async () => {
  const body = deferred<unknown>();
  const json = vi.fn(() => body.promise);
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({ ok: true, status: 200, json } as unknown as Response);
  const { createVm } = await import("./api");
  const { pendingVmCreations } = await import("./vm-creation-retries");
  const controller = new AbortController();
  const creation = createVm(vm.name, "container", controller.signal);
  const check = expect(creation).rejects.toMatchObject({ name: "AbortError" });
  await vi.waitFor(() => expect(json).toHaveBeenCalledOnce());
  controller.abort();
  body.resolve(succeeded);
  await check;
  expect(pendingVmCreations()).toHaveLength(1);
});

test("a creation's environment identity cannot change while polling the same receipt", async () => {
  vi.useFakeTimers();
  const other = { ...vm, id: "desk-" + "b".repeat(24) };
  const fetch = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(response({ ...succeeded, state: "queued", result: null }))
    .mockResolvedValueOnce(response({ ...succeeded, vm_id: other.id, result: other }));
  const { createVm } = await import("./api");
  const { pendingVmCreations } = await import("./vm-creation-retries");
  const check = expect(createVm(vm.name, "container")).rejects.toThrow("作成結果を確認できません");
  void check.catch(() => {});
  await vi.advanceTimersByTimeAsync(500);
  await check;
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(pendingVmCreations()).toHaveLength(1);
});

test("cancellation after a poll timer fires still prevents the queued status request", async () => {
  vi.useFakeTimers();
  const fetch = vi.spyOn(globalThis, "fetch")
    .mockResolvedValue(response({ ...succeeded, state: "queued", result: null }));
  const { createVm } = await import("./api");
  const { pendingVmCreations } = await import("./vm-creation-retries");
  const controller = new AbortController();
  const creation = createVm(vm.name, "container", controller.signal);
  const check = expect(creation).rejects.toMatchObject({ name: "AbortError" });
  await vi.advanceTimersByTimeAsync(0);
  const receipt = pendingVmCreations();
  expect(fetch).toHaveBeenCalledOnce();
  // Resolve the poll delay synchronously, then abort before its promise continuation.
  vi.advanceTimersByTime(500);
  controller.abort();
  await check;
  expect(fetch).toHaveBeenCalledOnce();
  expect(pendingVmCreations()).toEqual(receipt);
  expect(vi.getTimerCount()).toBe(0);
});

test("an abandoned request cannot start a fresh authentication check from its late 401", async () => {
  const pending = deferred<Response>();
  const fetch = vi.spyOn(globalThis, "fetch").mockReturnValueOnce(pending.promise)
    .mockResolvedValueOnce(response({ authenticated: false }));
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const { getVmStatus } = await import("./api");
  const controller = new AbortController();
  const check = expect(getVmStatus(controller.signal)).rejects.toMatchObject({ name: "AbortError" });
  controller.abort();
  pending.resolve(response({ detail: "expired" }, 401));
  await check;
  expect(fetch).toHaveBeenCalledOnce();
  expect(dispatch).not.toHaveBeenCalled();
});

test.each(["auth", "creation"])("cancelling after the %s body is read still abandons the unpublished result", async kind => {
  const body = deferred<unknown>();
  const json = vi.fn(() => body.promise);
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({ ok: true, status: 200, json } as unknown as Response);
  const api = await import("./api");
  const controller = new AbortController();
  const request = kind === "auth" ? api.getAuthStatus(controller.signal) : api.createVm(vm.name, "container", controller.signal);
  const outcome = request.catch(error => error);
  await vi.waitFor(() => expect(json).toHaveBeenCalledOnce());
  const cancelled = body.promise.then(() => controller.abort());
  body.resolve(kind === "auth" ? { authenticated: true } : succeeded);
  await cancelled;
  expect(await outcome).toMatchObject({ name: "AbortError" });
  if (kind === "creation") {
    const { pendingVmCreations } = await import("./vm-creation-retries");
    expect(pendingVmCreations()).toHaveLength(1);
  }
});
