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
      />
    );

    expect(screen.queryByText(/サブタスク/)).not.toBeInTheDocument();
  });

  it("applies correct CSS class for each state", () => {
    render(
      <StatusPanel
        state="executing"
        subtaskIndex={0}
        subtaskCount={0}
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
      />
    );

    const badge = screen.getByText("UNKNOWN_WEIRD_STATE");
    expect(badge.className).toContain("state-idle");
  });

  it("renders subtask list with progress marks", () => {
    render(
      <StatusPanel
        state="executing"
        subtaskIndex={1}
        subtaskCount={3}
        subtasks={[
          { id: "s1", description: "起動" },
          { id: "s2", description: "入力" },
          { id: "s3", description: "保存" },
        ]}
      />
    );

    const list = screen.getByTestId("subtask-list");
    expect(list).toBeInTheDocument();
    expect(screen.getByText("起動")).toBeInTheDocument();
    expect(screen.getByText("入力")).toBeInTheDocument();
    // 0件目は完了マーク、1件目が進行中
    expect(list.textContent).toContain("✓");
    expect(list.textContent).toContain("▶");
  });

  it("marks all done when completed", () => {
    render(
      <StatusPanel
        state="completed"
        subtaskIndex={2}
        subtaskCount={2}
        subtasks={[
          { id: "s1", description: "起動" },
          { id: "s2", description: "保存" },
        ]}
      />
    );

    expect(screen.getByTestId("subtask-list").textContent).not.toContain("▶");
  });
});
