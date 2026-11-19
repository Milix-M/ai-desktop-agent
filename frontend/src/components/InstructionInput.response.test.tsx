import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import InstructionInput from "./InstructionInput";
import { createTask } from "@/lib/api";
import type { PendingSubmission } from "@/lib/types";

afterEach(() => { cleanup(); sessionStorage.clear(); vi.restoreAllMocks(); });

test("an invalid acceptance response preserves the draft and reuses the same task receipt on retry", async () => {
  const valid = { session_id: "accepted", vm_id: "desktop", state: "executing", is_running: true,
    action_count: 0, success_count: 0, failure_count: 0, subtasks: [], current_subtask_index: 0 };
  const response = (body: unknown) => ({ ok: true, json: async () => body }) as Response;
  const fetch = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(response({ ...valid, is_running: "false" }))
    .mockResolvedValueOnce(response(valid));
  const submission = { current: null as PendingSubmission | null };
  render(<InstructionInput disabled={false} vmId="desktop" submission={submission}
    onSubmit={async (instruction, allowRestart, requestId) => { await createTask(instruction, "desktop", allowRestart, requestId); }} />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  const retry = await screen.findByRole("button", { name: "再送" });
  expect(screen.getByRole("textbox")).toHaveValue("報告書を保存する");
  const first = fetch.mock.calls[0][1]!;
  const key = (first.headers as Record<string, string>)["Idempotency-Key"];
  expect(submission.current?.requestId).toBe(key);
  fireEvent.click(retry);
  await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue(""));
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[1][1]).toMatchObject({ body: first.body, headers: { "Idempotency-Key": key } });
  expect(submission.current).toBeNull();
});
