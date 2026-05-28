import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import LogPanel from "@/components/LogPanel";
import type { LogEntry } from "@/lib/types";

describe("LogPanel", () => {
  it("renders placeholder when no entries", () => {
    render(<LogPanel entries={[]} />);

    expect(screen.getByText("待機中...")).toBeInTheDocument();
  });

  it("renders entries with correct time and message", () => {
    const entries: LogEntry[] = [
      { id: 1, time: "12:34", message: "テストメッセージ", level: "action" },
      { id: 2, time: "12:35", message: "エラー発生", level: "error" },
    ];

    render(<LogPanel entries={entries} />);

    expect(screen.getByText("テストメッセージ")).toBeInTheDocument();
    expect(screen.getByText("エラー発生")).toBeInTheDocument();
    expect(screen.getByText("12:34")).toBeInTheDocument();
    expect(screen.getByText("12:35")).toBeInTheDocument();
  });

  it("applies correct CSS class per level", () => {
    const entries: LogEntry[] = [
      { id: 1, time: "12:34", message: "action msg", level: "action" },
      { id: 2, time: "12:35", message: "error msg", level: "error" },
      { id: 3, time: "12:36", message: "state msg", level: "state" },
      { id: 4, time: "12:37", message: "complete msg", level: "complete" },
    ];

    const { container } = render(<LogPanel entries={entries} />);

    const entryDivs = container.querySelectorAll(".log .entry");
    expect(entryDivs).toHaveLength(4);
    expect(entryDivs[0].className).toContain("action");
    expect(entryDivs[1].className).toContain("error");
    expect(entryDivs[2].className).toContain("state");
    expect(entryDivs[3].className).toContain("complete");
  });

  it("escapes HTML in messages", () => {
    const entries: LogEntry[] = [
      { id: 1, time: "12:34", message: "<script>alert('xss')</script>", level: "action" },
    ];

    const { container } = render(<LogPanel entries={entries} />);

    // Should render as text, not execute — use getByText to find the literal escaped string
    expect(
      screen.getByText("<script>alert('xss')</script>")
    ).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
  });

  it("renders multiple entries in order", () => {
    const entries: LogEntry[] = [
      { id: 10, time: "12:40", message: "最後", level: "complete" },
      { id: 5, time: "12:30", message: "最初", level: "action" },
    ];

    const { container } = render(<LogPanel entries={entries} />);

    const entryDivs = container.querySelectorAll(".log .entry");
    expect(entryDivs[0].textContent).toContain("最後");
    expect(entryDivs[1].textContent).toContain("最初");
  });
});
