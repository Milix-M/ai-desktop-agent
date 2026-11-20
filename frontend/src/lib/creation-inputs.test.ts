import { afterEach, beforeEach, expect, test, vi } from "vitest";

beforeEach(() => { vi.resetModules(); sessionStorage.clear(); });
afterEach(() => { vi.restoreAllMocks(); });

test.each([
  [undefined, undefined, "qemu"],
  ["", "", "qemu"],
  ["日本語の作業環境", "CONTAINER", "container"],
] as const)("creation input %j / %j preserves normalized admission and retry identity", async (name, kind, expectedKind) => {
  const vm = { id: "saved-environment", name: name || "saved-environment", kind: expectedKind,
    status: "creating", health: null, managed: true, vnc_host: "saved-environment", vnc_port: 0, ws_port: 0 };
  const fetch = vi.spyOn(globalThis, "fetch")
    .mockRejectedValueOnce(new TypeError("acknowledgement lost"))
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({
      id: "creation-" + "a".repeat(64), vm_id: vm.id, state: "succeeded", result: vm,
      error: null, error_status: null,
    }) } as Response);
  const { createVm } = await import("./api");
  const { pendingVmCreations } = await import("./vm-creation-retries");
  await expect(createVm(name, kind)).rejects.toThrow("作成結果を確認できません");
  const first = fetch.mock.calls[0][1];
  expect(JSON.parse(first?.body as string)).toEqual({ name: name || null, kind: expectedKind });
  expect(pendingVmCreations()).toHaveLength(1);
  expect(await createVm(name, kind)).toEqual(vm);
  expect(fetch.mock.calls[1][1]?.headers).toEqual(first?.headers);
  expect(pendingVmCreations()).toEqual([]);
});

test("an oversized environment name is rejected without a request or a retry receipt", async () => {
  const fetch = vi.spyOn(globalThis, "fetch");
  const { createVm } = await import("./api");
  const { pendingVmCreations } = await import("./vm-creation-retries");
  await expect(createVm("名".repeat(129), "container")).rejects.toMatchObject({ status: 400 });
  expect(fetch).not.toHaveBeenCalled();
  expect(pendingVmCreations()).toEqual([]);
});
