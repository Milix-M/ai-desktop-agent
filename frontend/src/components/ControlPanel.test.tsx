import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ControlPanel from "@/components/ControlPanel";

describe("ControlPanel", () => {
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
