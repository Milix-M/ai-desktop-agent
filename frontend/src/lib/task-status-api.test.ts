import { afterEach, expect, test, vi } from "vitest";
import { createTask, getCurrentTask } from "./api";

const valid = { session_id: "task-1", vm_id: "desktop", state: "executing", is_running: true,
  action_count: 0, success_count: 0, failure_count: 0, subtasks: [], current_subtask_index: 0 };
afterEach(() => vi.restoreAllMocks());

test.each([null, [], {}, { ...valid, subtasks: null }, { ...valid, subtasks: undefined },
  { ...valid, current_subtask_index: undefined }, { ...valid, current_subtask_index: null }, { ...valid, is_running: "false" },
  { ...valid, revision: -1 }].map(value => [value] as const))("invalid HTTP task data cannot be published as accepted or restored state: %j", async value => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true,
    json: async () => JSON.parse(JSON.stringify(value)) } as Response);
  await expect(createTask("報告書を保存する", "desktop", false, "receipt-1")).rejects.toThrow("受付結果を確認できません");
  await expect(getCurrentTask("desktop")).rejects.toThrow("タスクの応答が不正です");
  expect(fetch).toHaveBeenCalledTimes(2);
});

test("valid task responses retain their typed fields and scoped query", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => valid } as Response);
  expect(await createTask("報告書を保存する", "desktop")).toEqual(valid);
  expect(await getCurrentTask("desktop")).toEqual(valid);
  expect(fetch.mock.calls[1][0]).toMatch(/\/tasks\/current\?vm_id=desktop$/);
});

test.each(["another-desktop", null, undefined])("a response for environment %j cannot acknowledge a request scoped to desktop", async vm_id => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => ({ ...valid, vm_id }) } as Response);
  await expect(createTask("報告書を保存する", "desktop", false, "receipt-1")).rejects.toThrow("受付結果を確認できません");
  await expect(getCurrentTask("desktop")).rejects.toThrow("タスクの応答が不正です");
});

test("an unscoped legacy request can still restore a status without environment metadata", async () => {
  const legacy = { ...valid, vm_id: undefined };
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => legacy } as Response);
  expect(await getCurrentTask(null)).toEqual(legacy);
});
