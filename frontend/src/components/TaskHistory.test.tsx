import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import TaskHistory from "@/components/TaskHistory";
import type { TaskHistoryItem } from "@/lib/types";

const ITEMS: TaskHistoryItem[] = [
  {
    id: "a1",
    instruction: "テスト指示1",
    state: "completed",
    success: true,
    action_count: 3,
    success_count: 3,
    failure_count: 0,
    updated_at: 1700000000,
  },
  {
    id: "b2",
    instruction: "テスト指示2",
    state: "interrupted",
    success: false,
    action_count: 1,
    success_count: 0,
    failure_count: 1,
    updated_at: 1700000100,
  },
];

describe("TaskHistory", () => {
  it("renders empty state when no items", () => {
    render(
      <TaskHistory items={[]} selectedId={null} onSelect={vi.fn()} onDelete={vi.fn()} />
    );
    expect(screen.getByText("履歴（0件）")).toBeInTheDocument();
    expect(screen.getByText("履歴なし")).toBeInTheDocument();
  });

  it("renders items with titles", () => {
    render(<TaskHistory items={ITEMS} selectedId={null} onSelect={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.getByText("テスト指示1")).toBeInTheDocument();
    expect(screen.getByText("テスト指示2")).toBeInTheDocument();
  });

  it("calls onSelect with task id", async () => {
    const onSelect = vi.fn();
    render(<TaskHistory items={ITEMS} selectedId={null} onSelect={onSelect} onDelete={vi.fn()} />);
    await userEvent.click(screen.getByText("テスト指示2"));
    expect(onSelect).toHaveBeenCalledWith("b2");
  });

  it("calls onDelete with task id", async () => {
    const onDelete = vi.fn();
    render(<TaskHistory items={ITEMS} selectedId={null} onSelect={vi.fn()} onDelete={onDelete} />);
    await userEvent.click(screen.getByRole("button", { name: "テスト指示2を削除" }));
    expect(onDelete).toHaveBeenCalledWith("b2");
  });

  it("shows decision usage when present", () => {
    const items: TaskHistoryItem[] = [
      {
        ...ITEMS[0],
        decision_calls: 2,
        decision_escalations: 1,
      },
    ];
    render(<TaskHistory items={items} selectedId={null} onSelect={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.getByText(/決定2件・退避1件/)).toBeInTheDocument();
  });

  it("hides decision usage when zero", () => {
    render(<TaskHistory items={ITEMS} selectedId={null} onSelect={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.queryByText(/決定/)).not.toBeInTheDocument();
  });
});
