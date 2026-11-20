import { afterEach, beforeEach, expect, test, vi } from "vitest";

type Api = typeof import("./api");
const operations = [
  ["history list", (api: Api) => api.getTaskHistory()],
  ["history detail", (api: Api) => api.getTaskDetail("saved")],
  ["history deletion", (api: Api) => api.deleteTask("saved")],
  ["task stop", (api: Api) => api.controlTask("stop", "desktop", "observed")],
  ["environment restart", (api: Api) => api.restartVm()],
] as const;
const response = (value: unknown, status = 200) => ({
  ok: status === 200, status, json: async () => value,
}) as Response;

beforeEach(() => { vi.resetModules(); vi.useFakeTimers(); localStorage.clear(); });
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

function stalled(signal: AbortSignal | null | undefined): Promise<never> {
  return new Promise((_, reject) => signal?.addEventListener("abort", () => {
    reject(new DOMException("aborted", "AbortError"));
  }, { once: true }));
}

test.each(operations)("%s bounds both header and response-body waits", async (_name, run) => {
  const api = await import("./api");
  for (const phase of ["headers", "body"] as const) {
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation((_url, init) => phase === "headers"
      ? stalled(init?.signal)
      : Promise.resolve({ ok: true, status: 200, json: () => stalled(init?.signal) } as unknown as Response));
    let settled = false;
    const outcome = run(api).then(value => ({ value }), error => ({ error }));
    void outcome.then(() => { settled = true; });
    await vi.advanceTimersByTimeAsync(29999);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(settled).toBe(true);
    expect(await outcome).toMatchObject({ error: { message: expect.stringContaining("応答を確認できません") } });
    expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(fetch).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
    fetch.mockRestore();
  }
});

test.each([false, true])("caller cancellation stays distinct from a deadline (already aborted: %s)", async alreadyAborted => {
  const fetch = vi.spyOn(globalThis, "fetch").mockImplementation((_url, init) => stalled(init?.signal));
  const api = await import("./api");
  const controller = new AbortController();
  if (alreadyAborted) controller.abort();
  const remove = vi.spyOn(controller.signal, "removeEventListener");
  const outcome = api.getVmStatus(controller.signal).catch(error => error);
  controller.abort();
  expect(await outcome).toMatchObject({ name: "AbortError" });
  expect(fetch).toHaveBeenCalledTimes(alreadyAborted ? 0 : 1);
  expect(remove).toHaveBeenCalledWith("abort", expect.any(Function));
  expect(vi.getTimerCount()).toBe(0);
});

test("a response arriving after the deadline cannot trigger authentication or publish success", async () => {
  let finish!: (value: Response) => void;
  const fetch = vi.spyOn(globalThis, "fetch").mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const api = await import("./api");
  const outcome = api.getVms().catch(error => error);
  await vi.advanceTimersByTimeAsync(30000);
  expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(true);
  finish(response({ detail: "late expiration" }, 401));
  expect(await outcome).toMatchObject({ message: expect.stringContaining("応答を確認できません") });
  expect(fetch).toHaveBeenCalledOnce();
  expect(dispatch).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(0);
});

test("a caller signal cannot leave a request unbounded and is not itself aborted by its deadline", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation((_url, init) => stalled(init?.signal));
  const api = await import("./api");
  const controller = new AbortController();
  const outcome = api.getVmStatus(controller.signal).catch(error => error);
  await vi.advanceTimersByTimeAsync(30000);
  expect(await outcome).toMatchObject({ message: expect.stringContaining("応答を確認できません") });
  expect(controller.signal.aborted).toBe(false);
  expect(vi.getTimerCount()).toBe(0);
});

test("successful and failed requests release timers and caller listeners without cancelling their caller", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response({ running: true, status: "running", health: null, name: "desktop" }))
    .mockRejectedValueOnce(new TypeError("offline"));
  const api = await import("./api");
  const controller = new AbortController();
  const remove = vi.spyOn(controller.signal, "removeEventListener");
  expect(await api.getVmStatus(controller.signal)).toMatchObject({ status: "running" });
  await expect(api.getVms()).rejects.toThrow("offline");
  expect(remove).toHaveBeenCalledWith("abort", expect.any(Function));
  expect(controller.signal.aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(30000);
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(vi.getTimerCount()).toBe(0);
});
