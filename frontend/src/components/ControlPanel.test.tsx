import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ControlPanel from "@/components/ControlPanel";

describe("ControlPanel", () => {
  it("shows the manual request and requires a screen-sharing resume", async () => {
    const onControl = vi.fn().mockResolvedValue(undefined);
    render(<ControlPanel onControl={onControl} state="awaiting_user" manualInput={{ id: "request-1", reason: "パスワードを入力してください" }} />);
    expect(screen.getByRole("status")).toHaveTextContent("パスワードを入力してください");
    expect(screen.queryByRole("button", { name: "一時停止" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "画面を送信して再開" }));
    expect(onControl).toHaveBeenCalledWith("resume");
    expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  });

  it("does not allow manual resume without the current request", () => {
    render(<ControlPanel onControl={vi.fn()} state="awaiting_user" />);
    expect(screen.getByRole("button", { name: "画面を送信して再開" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  });

  it("keeps stop available but suspends manual instructions and screen sharing until the request is synchronized", async () => {
    const onControl = vi.fn();
    const props = { onControl, state: "awaiting_user", manualInput: { id: "request-1", reason: "接続先を確認してください" } };
    const { rerender } = render(<ControlPanel {...props} manualInputReady={false} />);
    expect(screen.getByText(props.manualInput.reason)).toBeVisible();
    expect(screen.queryByText(/秘密情報を画面から隠して/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "画面を送信して再開" }));
    expect(onControl).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "停止" }));
    expect(onControl).toHaveBeenCalledExactlyOnceWith("stop");
    rerender(<ControlPanel {...props} manualInputReady />);
    expect(screen.getByText(/秘密情報を画面から隠して/)).toBeVisible();
    expect(screen.getByRole("button", { name: "画面を送信して再開" })).toBeEnabled();
  });

  it("disables pause and resume when idle", () => {
    render(<ControlPanel onControl={vi.fn()} state="idle" />);

    expect(screen.getByRole("button", { name: "一時停止" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "再開" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  });

  it("enables pause and stop when executing", () => {
    render(<ControlPanel onControl={vi.fn()} state="executing" />);

    expect(screen.getByRole("button", { name: "一時停止" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "再開" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "停止" })).not.toBeDisabled();
  });

  it("enables resume and stop when paused", () => {
    render(<ControlPanel onControl={vi.fn()} state="paused" />);

    expect(screen.getByRole("button", { name: "一時停止" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "再開" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "停止" })).not.toBeDisabled();
  });

  it("calls onControl with correct action", async () => {
    const onControl = vi.fn().mockResolvedValue(undefined);
    render(<ControlPanel onControl={onControl} state="executing" />);

    await userEvent.click(screen.getByRole("button", { name: "一時停止" }));
    expect(onControl).toHaveBeenCalledWith("pause");

    await userEvent.click(screen.getByRole("button", { name: "停止" }));
    expect(onControl).toHaveBeenCalledWith("stop");
  });

  it("calls onControl with 'resume' from paused state", async () => {
    const onControl = vi.fn().mockResolvedValue(undefined);
    render(<ControlPanel onControl={onControl} state="paused" />);

    await userEvent.click(screen.getByRole("button", { name: "再開" }));
    expect(onControl).toHaveBeenCalledWith("resume");
  });

  it("enables stop for all running-like states", () => {
    const runningStates = [
      "understanding",
      "planning",
      "executing",
      "waiting",
      "verifying",
      "recovering",
    ];

    for (const state of runningStates) {
      const { unmount } = render(
        <ControlPanel onControl={vi.fn()} state={state} />
      );
      expect(
        screen.getByRole("button", { name: "停止" })
      ).not.toBeDisabled();
      unmount();
    }
  });
});
