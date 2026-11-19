import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const vm = { id: "desk-" + "a".repeat(24), name: "作業環境", kind: "container", status: "creating", health: null, managed: true, vnc_host: "desktop", vnc_port: 0, ws_port: 0, adb_port: 0 };
const jobId = "creation-" + "a".repeat(64);
const queued = { id: jobId, vm_id: vm.id, state: "queued", result: null, error: null, error_status: null };
const succeeded = { ...queued, state: "succeeded", result: vm };
const response = (value: unknown) => ({ ok: true, status: 200, json: async () => value }) as Response;

describe("VM creation retry identities", () => {
  beforeEach(() => { vi.resetModules(); vi.useFakeTimers(); sessionStorage.clear(); });
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

  it("polls an accepted creation and removes its retry identity only after completion", async () => {
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response(queued))
      .mockResolvedValueOnce(response(succeeded));
    const { createVm } = await import("./api");
    const { pendingVmCreations } = await import("./vm-creation-retries");
    const creation = createVm(vm.name, "container");
    expect(pendingVmCreations()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(500);
    expect(await creation).toEqual(vm);
    expect(fetch.mock.calls[0][0]).toContain("/vm-creations");
    expect(fetch.mock.calls[0][1]?.headers).toMatchObject({ "Idempotency-Key": expect.stringMatching(/^[0-9a-f]{32}$/) });
    expect(fetch.mock.calls[1][0]).toContain("/vm-creations/" + jobId);
    expect(fetch.mock.calls[1][1]?.cache).toBe("no-store");
    expect(pendingVmCreations()).toEqual([]);
    await vi.advanceTimersByTimeAsync(1); // fake-timers schedules storage events created during a tick one millisecond later.
    expect(vi.getTimerCount()).toBe(0);
  });

  it("reuses the same key after a lost response and a module reload", async () => {
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("connection lost"))
      .mockResolvedValueOnce(response(succeeded));
    const first = await import("./api");
    await expect(first.createVm(vm.name, "container")).rejects.toThrow("同じ内容でもう一度操作");
    const key = (fetch.mock.calls[0][1]?.headers as Record<string, string>)["Idempotency-Key"];
    vi.resetModules();
    const second = await import("./api");
    expect(await second.createVm(vm.name, "container")).toEqual(vm);
    expect(fetch.mock.calls[1][1]?.headers).toMatchObject({ "Idempotency-Key": key });
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not treat a broken successful response as a definite failed creation", async () => {
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response({ ...succeeded, result: null }))
      .mockResolvedValueOnce(response(succeeded));
    const { createVm } = await import("./api");
    await expect(createVm(vm.name, "container")).rejects.toThrow("作成結果を確認できません");
    await createVm(vm.name, "container");
    expect(fetch.mock.calls[0][1]?.headers).toEqual(fetch.mock.calls[1][1]?.headers);
  });

  it("retains uncertain HTTP failures but clears a recorded terminal failure", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({ detail: "保存先を確認" }) } as Response)
      .mockResolvedValueOnce(response({ ...queued, state: "failed", error: "作成を中断", error_status: 503 }));
    const { createVm } = await import("./api");
    const { pendingVmCreations } = await import("./vm-creation-retries");
    await expect(createVm(vm.name, "container")).rejects.toMatchObject({ status: 503 });
    expect(pendingVmCreations()).toHaveLength(1);
    await expect(createVm(vm.name, "container")).rejects.toMatchObject({ status: 503 });
    expect(pendingVmCreations()).toEqual([]);
  });

  it("bounds each request and keeps the identity when an acknowledgement never arrives", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((_url, init) => new Promise((_, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }));
    const { createVm } = await import("./api");
    const { pendingVmCreations } = await import("./vm-creation-retries");
    const check = expect(createVm(vm.name, "container")).rejects.toThrow("作成結果を確認できません");
    await vi.advanceTimersByTimeAsync(10000);
    await check;
    expect(pendingVmCreations()).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("ends polling after ten minutes and retains its retry identity", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response(queued));
    const { createVm } = await import("./api");
    const { pendingVmCreations } = await import("./vm-creation-retries");
    const check = expect(createVm(vm.name, "container")).rejects.toThrow("作成結果を確認できません");
    await vi.advanceTimersByTimeAsync(600000);
    await check;
    expect(pendingVmCreations()).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("keeps the in-page identity when browser storage is unavailable", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("disabled"); });
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("lost"))
      .mockResolvedValueOnce(response(succeeded));
    const { createVm } = await import("./api");
    await expect(createVm(vm.name, "container")).rejects.toThrow();
    await createVm(vm.name, "container");
    expect(fetch.mock.calls[0][1]?.headers).toEqual(fetch.mock.calls[1][1]?.headers);
  });

  it("aborts local polling without discarding the server-owned creation", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response(queued));
    const { createVm } = await import("./api");
    const { pendingVmCreations } = await import("./vm-creation-retries");
    const controller = new AbortController();
    const check = expect(createVm(vm.name, "container", controller.signal)).rejects.toMatchObject({ name: "AbortError" });
    await vi.advanceTimersByTimeAsync(0);
    controller.abort();
    await check;
    expect(pendingVmCreations()).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(["queued", "succeeded", "failed"])(
    "cancellation after parsing a %s acknowledgement retains the receipt for an explicit retry",
    async state => {
      const record = state === "succeeded" ? succeeded : state === "failed"
        ? { ...queued, state, error: "環境を作成できません", error_status: 503 } : queued;
      let resolveBody!: (value: unknown) => void;
      const body = new Promise<unknown>(resolve => { resolveBody = resolve; });
      const json = vi.fn(() => body);
      const fetch = vi.spyOn(globalThis, "fetch")
        .mockResolvedValueOnce({ ok: true, status: 200, json } as unknown as Response)
        .mockResolvedValueOnce(response(state === "failed" ? record : succeeded));
      const { createVm } = await import("./api");
      const { pendingVmCreations } = await import("./vm-creation-retries");
      const controller = new AbortController();
      const outcome = createVm(vm.name, "container", controller.signal).catch(error => error);
      try {
        await Promise.resolve();
        expect(json).toHaveBeenCalledOnce();
        // The body is already parsed when cancellation reaches the caller's
        // microtask; no in-flight HTTP request remains to reject it.
        const cancellation = body.then(() => Promise.resolve().then(() => controller.abort()));
        resolveBody(record);
        await cancellation;
        expect(await outcome).toMatchObject({ name: "AbortError" });
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(pendingVmCreations()).toHaveLength(1);
        await vi.advanceTimersByTimeAsync(0);
        expect(vi.getTimerCount()).toBe(0);

        if (state === "failed") {
          await expect(createVm(vm.name, "container")).rejects.toMatchObject({ status: 503 });
        } else {
          expect(await createVm(vm.name, "container")).toEqual(vm);
        }
        expect(fetch.mock.calls[1][1]?.headers).toEqual(fetch.mock.calls[0][1]?.headers);
        expect(pendingVmCreations()).toEqual([]);
      } finally {
        resolveBody(record);
        controller.abort();
        await vi.runOnlyPendingTimersAsync();
        await outcome;
      }
    },
  );

  it.each(["truncated {", "{}", "[null]", '[{"key":"invalid","name":null,"kind":"container"}]'])(
    "damaged stored creations retain an existing retry key: %s", async stored => {
      const { vmCreationRetry, pendingVmCreations, forgetVmCreation } = await import("./vm-creation-retries");
      const original = vmCreationRetry("作業環境", "container");
      sessionStorage.setItem("ai-desktop-agent-vm-creations", stored);
      expect(vmCreationRetry("作業環境", "container")).toEqual(original);
      const snapshot = pendingVmCreations();
      snapshot[0].key = "modified-outside-the-store";
      expect(pendingVmCreations()[0].key).toBe(original.key);
      forgetVmCreation(original.key);
      expect(pendingVmCreations()).toEqual([]);
      expect(vmCreationRetry("作業環境", "container").key).not.toBe(original.key);
    });
});
