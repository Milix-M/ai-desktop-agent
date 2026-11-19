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

test.each(["truncated {", "{}", "[null]", "[42]", '[{"fingerprint":"invalid","requestId":"invalid"}]'])(
  "damaged browser receipts retain the current in-memory retry identity: %s", async stored => {
    const retries = await import("./task-submission-retries");
    await retries.taskSubmissionRetry(original);
    sessionStorage.setItem("ai-desktop-agent-task-submissions", stored);
    expect(await retries.taskSubmissionRetry({ ...original, requestId: "b".repeat(32) })).toBe(original.requestId);
    retries.forgetTaskSubmission(original.requestId);
    expect(await retries.taskSubmissionRetry({ ...original, requestId: "c".repeat(32) })).toBe("c".repeat(32));
    expect(sessionStorage.getItem("ai-desktop-agent-task-submissions")).not.toContain(original.instruction);
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

test("a failed hash after logout cannot fall back to the old retry identity", async () => {
  let fail!: (error: Error) => void;
  vi.spyOn(crypto.subtle, "digest").mockReturnValue(new Promise((_, reject) => { fail = reject; }));
  const retries = await import("./task-submission-retries");
  const preparing = retries.taskSubmissionRetry(original);
  const rejected = expect(preparing).rejects.toMatchObject({ name: "AbortError" });
  retries.clearTaskSubmissions();
  fail(new Error("Web Crypto unavailable"));
  await rejected;
  expect(JSON.stringify(sessionStorage)).not.toContain(original.requestId);
});

test.each([
  ["fingerprint", "array"], ["fingerprint", "nested-array"],
  ["requestId", "array"], ["requestId", "nested-array"],
] as const)("a stored %s represented as %s cannot replace the in-memory receipt or survive completion", async (field, shape) => {
  const retries = await import("./task-submission-retries");
  await retries.taskSubmissionRetry(original);
  const key = "ai-desktop-agent-task-submissions";
  const stored = JSON.parse(sessionStorage.getItem(key)!);
  stored[0][field] = shape === "array" ? [stored[0][field]] : [[stored[0][field]]];
  sessionStorage.setItem(key, JSON.stringify(stored));
  expect(await retries.taskSubmissionRetry({ ...original, requestId: "b".repeat(32) })).toBe(original.requestId);
  retries.forgetTaskSubmission(original.requestId);
  const fresh = { ...original, requestId: "c".repeat(32) };
  expect(await retries.taskSubmissionRetry(fresh)).toBe(fresh.requestId);
  const saved = JSON.parse(sessionStorage.getItem(key)!);
  expect(saved).toHaveLength(1);
  expect(saved[0]).toEqual({ fingerprint: expect.any(String), requestId: fresh.requestId });
  expect(JSON.stringify(saved)).not.toContain(original.instruction);
});
