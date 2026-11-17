import { webcrypto } from "node:crypto";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { PendingSubmission } from "./types";

const original: PendingSubmission = {
  instruction: "議事録を保存してください", vmId: "private-workspace", allowVmRestart: false, requestId: "a".repeat(32),
};

beforeEach(() => { vi.resetModules(); sessionStorage.clear(); vi.stubGlobal("crypto", webcrypto); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

test("reloading preserves the receipt without storing the instruction or environment name", async () => {
  const first = await import("./task-submission-retries");
  expect(await first.taskSubmissionRetry(original)).toBe(original.requestId);
  const stored = JSON.stringify(sessionStorage);
  expect(stored).not.toContain(original.instruction);
  expect(stored).not.toContain(original.vmId);
  vi.resetModules();
  const reloaded = await import("./task-submission-retries");
  expect(await reloaded.taskSubmissionRetry({ ...original, requestId: "b".repeat(32) })).toBe(original.requestId);
});

test.each([
  { instruction: "見積書を保存してください" }, { vmId: "another" }, { allowVmRestart: true },
])("different request fields keep distinct receipts: %s", async change => {
  const retries = await import("./task-submission-retries");
  await retries.taskSubmissionRetry(original);
  const changed = { ...original, ...change, requestId: "b".repeat(32) };
  expect(await retries.taskSubmissionRetry(changed)).toBe(changed.requestId);
  expect(await retries.taskSubmissionRetry({ ...original, requestId: "c".repeat(32) })).toBe(original.requestId);
});

test("concurrent preparations for the same payload share an identity", async () => {
  const retries = await import("./task-submission-retries");
  const ids = await Promise.all([
    retries.taskSubmissionRetry(original),
    retries.taskSubmissionRetry({ ...original, requestId: "b".repeat(32) }),
  ]);
  expect(ids[0]).toBe(ids[1]);
});

test("an explicit new execution survives late completion of an older request", async () => {
  const retries = await import("./task-submission-retries");
  await retries.taskSubmissionRetry(original);
  const fresh = { ...original, requestId: "b".repeat(32) };
  expect(await retries.taskSubmissionRetry(fresh, true)).toBe(fresh.requestId);
  retries.forgetTaskSubmission(original.requestId);
  expect(await retries.taskSubmissionRetry(original)).toBe(fresh.requestId);
  retries.forgetTaskSubmission(fresh.requestId);
  expect(await retries.taskSubmissionRetry(original)).toBe(original.requestId);
});

test("logout removes pending identities", async () => {
  const retries = await import("./task-submission-retries");
  await retries.taskSubmissionRetry(original);
  retries.clearTaskSubmissions();
  expect(await retries.taskSubmissionRetry({ ...original, requestId: "b".repeat(32) })).toBe("b".repeat(32));
});

test("disabled storage keeps the retry identity in memory", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("disabled"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("disabled"); });
  const retries = await import("./task-submission-retries");
  await retries.taskSubmissionRetry(original);
  expect(await retries.taskSubmissionRetry({ ...original, requestId: "b".repeat(32) })).toBe(original.requestId);
});

test("a preparation finishing after logout cannot restore a receipt or submit", async () => {
  let finish!: (value: ArrayBuffer) => void;
  vi.spyOn(crypto.subtle, "digest").mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const retries = await import("./task-submission-retries");
  const preparing = retries.taskSubmissionRetry(original);
  const rejected = expect(preparing).rejects.toMatchObject({ name: "AbortError" });
  retries.clearTaskSubmissions();
  finish(new ArrayBuffer(32));
  await rejected;
  expect(JSON.stringify(sessionStorage)).not.toContain(original.requestId);
});
