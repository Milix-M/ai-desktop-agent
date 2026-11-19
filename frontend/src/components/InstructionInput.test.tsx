import { webcrypto } from "node:crypto";
import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { act, render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import InstructionInput from "@/components/InstructionInput";
import { ApiError } from "@/lib/api";
import type { PendingSubmission } from "@/lib/types";

beforeEach(() => { sessionStorage.clear(); vi.stubGlobal("crypto", webcrypto); });
afterEach(() => vi.unstubAllGlobals());

it("a newer mounted input can take shared submission ownership while another input is hashing", async () => {
  let finish!: (value: ArrayBuffer) => void;
  const hashing = vi.spyOn(crypto.subtle, "digest").mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  const submission = { current: null as PendingSubmission | null };
  const first = vi.fn().mockResolvedValue(undefined);
  const second = vi.fn().mockResolvedValue(undefined);
  try {
    render(<>
      <InstructionInput onSubmit={first} disabled={false} initialValue="最初の文書を保存する" vmId="first" submission={submission} />
      <InstructionInput onSubmit={second} disabled={false} initialValue="別の文書を保存する" vmId="second" submission={submission} />
    </>);
    const inputs = screen.getAllByRole("textbox");
    const buttons = screen.getAllByRole("button", { name: "実行" });
    fireEvent.click(buttons[0]);
    fireEvent.click(buttons[1]);
    await vi.waitFor(() => expect(second).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(inputs[1]).toHaveValue(""));
    await act(async () => finish(new ArrayBuffer(32)));
    expect(first).not.toHaveBeenCalled();
    expect(inputs[0]).toHaveValue("最初の文書を保存する");
    expect(inputs[0]).toBeEnabled();
  } finally { hashing.mockRestore(); }
});

it.each(["resolve", "reject"])("unmounting during request preparation prevents a later submission: %s", async outcome => {
  let resolve!: (value: ArrayBuffer) => void;
  let reject!: (error: Error) => void;
  const hashing = vi.spyOn(crypto.subtle, "digest").mockReturnValueOnce(new Promise((done, fail) => {
    resolve = done; reject = fail;
  }));
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  const submission = { current: null as PendingSubmission | null };
  try {
    const view = render(<InstructionInput onSubmit={onSubmit} disabled={false} submission={submission} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "報告書を保存する" } });
    fireEvent.click(screen.getByRole("button", { name: "実行" }));
    expect(hashing).toHaveBeenCalledOnce();
    const attempt = submission.current;
    view.unmount();
    await act(async () => {
      if (outcome === "resolve") resolve(new ArrayBuffer(32));
      else reject(new Error("Web Crypto unavailable"));
    });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(submission.current).toBe(attempt);
  } finally { hashing.mockRestore(); }
});

it("preserves the instruction and retry identity after task admission returns 429", async () => {
  const onSubmit = vi.fn().mockRejectedValueOnce(new ApiError(429, "60秒後にもう一度送信してください"))
    .mockResolvedValue(undefined);
  render(<InstructionInput onSubmit={onSubmit} disabled={false} vmId="desktop" />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  const retry = await screen.findByRole("button", { name: "再送" });
  expect(screen.getByRole("textbox")).toHaveValue("報告書を保存する");
  expect(onSubmit).toHaveBeenCalledTimes(1);
  fireEvent.click(retry);
  await vi.waitFor(() => expect(screen.getByRole("textbox")).toHaveValue(""));
  expect(onSubmit.mock.calls[1]).toEqual(onSubmit.mock.calls[0]);
});

describe("InstructionInput", () => {
  it("renders textarea and submit button", () => {
    render(<InstructionInput onSubmit={vi.fn()} disabled={false} />);

    expect(screen.getByPlaceholderText(/LibreOffice/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "実行" })).toBeInTheDocument();
  });

  it("disables submit button when textarea is empty", () => {
    render(<InstructionInput onSubmit={vi.fn()} disabled={false} />);
    expect(screen.getByRole("button")).toBeDisabled();
  });

  it("enables submit button when text is entered", async () => {
    render(<InstructionInput onSubmit={vi.fn()} disabled={false} />);

    await userEvent.type(
      screen.getByPlaceholderText(/LibreOffice/),
      "テスト指示"
    );

    expect(screen.getByRole("button")).not.toBeDisabled();
  });

  it("calls onSubmit and clears input on button click", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<InstructionInput onSubmit={onSubmit} disabled={false} />);

    const textarea = screen.getByPlaceholderText(/LibreOffice/);
    await userEvent.type(textarea, "テスト指示");
    await userEvent.click(screen.getByRole("button"));

    await vi.waitFor(() => expect(textarea).toHaveValue(""));
    expect(onSubmit).toHaveBeenCalledWith("テスト指示", false, expect.stringMatching(/^[a-f0-9]{32}$/));
  });

  it("calls onSubmit on Enter key (without Shift)", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<InstructionInput onSubmit={onSubmit} disabled={false} />);

    const textarea = screen.getByPlaceholderText(/LibreOffice/);
    await userEvent.type(textarea, "テスト指示");
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledWith("テスト指示", false, expect.stringMatching(/^[a-f0-9]{32}$/)));
  });

  it("does NOT call onSubmit on Shift+Enter", async () => {
    const onSubmit = vi.fn();
    render(<InstructionInput onSubmit={onSubmit} disabled={false} />);

    const textarea = screen.getByPlaceholderText(/LibreOffice/);
    await userEvent.type(textarea, "テスト指示");
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });

    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("passes allowVmRestart flag when checked", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<InstructionInput onSubmit={onSubmit} disabled={false} />);

    await userEvent.click(screen.getByRole("checkbox"));
    const textarea = screen.getByPlaceholderText(/LibreOffice/);
    await userEvent.type(textarea, "テスト指示");
    await userEvent.click(screen.getByRole("button", { name: "実行" }));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledWith("テスト指示", true, expect.stringMatching(/^[a-f0-9]{32}$/)));
  });

  it("disables everything when disabled=true", () => {
    render(<InstructionInput onSubmit={vi.fn()} disabled={true} />);

    expect(screen.getByPlaceholderText(/LibreOffice/)).toBeDisabled();
    expect(screen.getByRole("button")).toBeDisabled();
    expect(screen.getByRole("button")).toHaveTextContent("実行");
  });
});

it("keeps the instruction after failure and blocks duplicate submissions", async () => {
  let reject: (reason: Error) => void = () => {};
  const onSubmit = vi.fn(() => new Promise<void>((_, fail) => { reject = fail; }));
  render(<InstructionInput onSubmit={onSubmit} disabled={false} />);
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "文書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  fireEvent.keyDown(input, { key: "Enter" });
  await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
  expect(input).toBeDisabled();
  reject(new Error("接続できません"));
  await vi.waitFor(() => expect(input).toBeEnabled());
  expect(input).toHaveValue("文書を保存する");
});

it("does not submit Enter while Japanese IME composition is active", () => {
  const onSubmit = vi.fn();
  render(<InstructionInput onSubmit={onSubmit} disabled={false} />);
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "報告書" } });
  fireEvent.keyDown(input, { key: "Enter", isComposing: true });
  expect(onSubmit).not.toHaveBeenCalled();
});

it("reuses the retry identity after an uncertain response and renews it after success", async () => {
  const onSubmit = vi.fn().mockRejectedValueOnce(new Error("response lost")).mockResolvedValue(undefined);
  render(<InstructionInput onSubmit={onSubmit} disabled={false} vmId="desktop" />);
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  fireEvent.click(await screen.findByRole("button", { name: "再送" }));
  await vi.waitFor(() => expect(input).toHaveValue(""));
  const first = onSubmit.mock.calls[0][2];
  expect(onSubmit.mock.calls[1][2]).toBe(first);
  fireEvent.change(input, { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(3));
  expect(onSubmit.mock.calls[2][2]).not.toBe(first);
});

it.each(["instruction", "vm", "restart"])("uses a new identity when %s changes", async (change) => {
  const onSubmit = vi.fn().mockRejectedValue(new Error("response lost"));
  const { rerender } = render(<InstructionInput onSubmit={onSubmit} disabled={false} vmId="desktop" />);
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  await screen.findByRole("button", { name: "再送" });
  if (change === "instruction") fireEvent.change(input, { target: { value: "見積書を保存する" } });
  if (change === "restart") fireEvent.click(screen.getByRole("checkbox"));
  if (change === "vm") rerender(<InstructionInput onSubmit={onSubmit} disabled={false} vmId="second" />);
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
  expect(onSubmit.mock.calls[1][2]).not.toBe(onSubmit.mock.calls[0][2]);
});

it("requires an explicit new-task click after an accepted task's history was deleted", async () => {
  const onSubmit = vi.fn().mockRejectedValueOnce(new ApiError(410, "受付済み・履歴削除済み")).mockResolvedValue(undefined);
  render(<InstructionInput onSubmit={onSubmit} disabled={false} />);
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  const fresh = await screen.findByRole("button", { name: "新しいタスクとして実行" });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledTimes(1);
  fireEvent.click(fresh);
  await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
  expect(onSubmit.mock.calls[1][2]).not.toBe(onSubmit.mock.calls[0][2]);
});

it("keeps the identity and restart permission across reauthentication remounts", async () => {
  const submission = { current: null as PendingSubmission | null };
  const onSubmit = vi.fn().mockRejectedValue(new Error("response lost"));
  const first = render(<InstructionInput onSubmit={onSubmit} disabled={false} vmId="desktop" submission={submission} />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  await screen.findByRole("button", { name: "再送" });
  first.unmount();
  render(<InstructionInput onSubmit={onSubmit} disabled={false} vmId="desktop" submission={submission} initialValue="報告書を保存する" />);
  expect(screen.getByRole("checkbox")).toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: "再送" }));
  await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
  expect(onSubmit.mock.calls[1][2]).toBe(onSubmit.mock.calls[0][2]);
});

it("an old mounted request cannot erase a draft after reauthentication", async () => {
  let resolve!: () => void;
  const onSubmit = vi.fn(() => new Promise<void>((done) => { resolve = done; }));
  const draftChanged = vi.fn();
  const submission = { current: null as PendingSubmission | null };
  const first = render(<InstructionInput onSubmit={onSubmit} disabled={false} submission={submission} onDraftChange={draftChanged} />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "元の報告書" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
  first.unmount();
  draftChanged.mockClear();
  resolve();
  await Promise.resolve();
  expect(draftChanged).not.toHaveBeenCalled();
  expect(submission.current?.instruction).toBe("元の報告書");
});

it("a fresh page reuses the persisted identity and requires an explicit new execution after 410", async () => {
  const onSubmit = vi.fn().mockRejectedValueOnce(new Error("response lost"))
    .mockRejectedValueOnce(new ApiError(410, "受付済み・履歴削除済み"))
    .mockResolvedValue(undefined);
  const first = render(<InstructionInput onSubmit={onSubmit} disabled={false} vmId="desktop" />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  await screen.findByRole("button", { name: "再送" });
  first.unmount();
  render(<InstructionInput onSubmit={onSubmit} disabled={false} vmId="desktop" />);
  expect(screen.getByRole("textbox")).toHaveValue("");
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "報告書を保存する" } });
  fireEvent.click(screen.getByRole("button", { name: "実行" }));
  const fresh = await screen.findByRole("button", { name: "新しいタスクとして実行" });
  expect(onSubmit.mock.calls[1][2]).toBe(onSubmit.mock.calls[0][2]);
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledTimes(2);
  fireEvent.click(fresh);
  await vi.waitFor(() => expect(screen.getByRole("textbox")).toHaveValue(""));
  expect(onSubmit.mock.calls[2][2]).not.toBe(onSubmit.mock.calls[0][2]);
});
