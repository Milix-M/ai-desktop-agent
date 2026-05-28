import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import StatusPanel from "@/components/StatusPanel";

describe("StatusPanel", () => {
  it("renders state badge", () => {
    render(
      <StatusPanel
        state="idle"
        subtaskIndex={0}
        subtaskCount={0}
        actionCount={0}
        successCount={0}
        failureCount={0}
      />
    );

    expect(screen.getByText("IDLE")).toBeInTheDocument();
  });

  it("shows subtask info when count > 0", () => {
    render(
      <StatusPanel
        state="executing"
        subtaskIndex={2}
        subtaskCount={5}
        actionCount={10}
        successCount={8}
        failureCount={2}
      />
    );

    expect(screen.getByText("EXECUTING")).toBeInTheDocument();
    expect(screen.getByText("サブタスク 3/5")).toBeInTheDocument();
  });

  it("does not show subtask info when count is 0", () => {
    render(
      <StatusPanel
        state="idle"
        subtaskIndex={0}
        subtaskCount={0}
        actionCount={0}
        successCount={0}
        failureCount={0}
      />
    );

    expect(screen.queryByText(/サブタスク/)).not.toBeInTheDocument();
  });

  it("renders stat values", () => {
    render(
      <StatusPanel
        state="executing"
        subtaskIndex={0}
        subtaskCount={3}
        actionCount={42}
        successCount={38}
        failureCount={4}
      />
    );

    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("38")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
  });

  it("applies correct CSS class for each state", () => {
    const { container } = render(
      <StatusPanel
        state="executing"
        subtaskIndex={0}
        subtaskCount={0}
        actionCount={0}
        successCount={0}
        failureCount={0}
      />
    );

    const badge = screen.getByText("EXECUTING");
    expect(badge.className).toContain("state-executing");
  });

  it("uses fallback class for unknown state", () => {
    render(
      <StatusPanel
        state="unknown_weird_state"
        subtaskIndex={0}
        subtaskCount={0}
        actionCount={0}
        successCount={0}
        failureCount={0}
      />
    );

    const badge = screen.getByText("UNKNOWN_WEIRD_STATE");
    expect(badge.className).toContain("state-idle");
  });
});
