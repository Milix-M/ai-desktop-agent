import { describe, it, expect, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
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
  it("shows a non-Error deletion failure and permits a deliberate retry", async () => {
    const onDelete = vi.fn().mockRejectedValueOnce("保存先が応答しません").mockResolvedValueOnce(undefined);
    render(<TaskHistory items={ITEMS} selectedId="a1" onSelect={vi.fn()} onDelete={onDelete} />);
    const remove = screen.getByRole("button", { name: "テスト指示1を削除" });
    await userEvent.click(remove);
    expect(await screen.findByRole("alert")).toHaveTextContent("削除を確認できません: 保存先が応答しません");
    expect(remove).toBeEnabled();
    await userEvent.click(remove);
    await vi.waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(onDelete.mock.calls).toEqual([["a1"], ["a1"]]);
  });

  it.each(["success", "failure"])("a late deletion %s cannot alter a newly mounted history", async outcome => {
    let resolve!: () => void;
    let reject!: (error: Error) => void;
    const onDelete = vi.fn(() => new Promise<void>((done, fail) => { resolve = done; reject = fail; }));
    const previous = render(<TaskHistory items={ITEMS} selectedId="a1" onSelect={vi.fn()} onDelete={onDelete} />);
    await userEvent.click(screen.getByRole("button", { name: "テスト指示1を削除" }));
    expect(onDelete).toHaveBeenCalledOnce();
    previous.unmount();
    const nextDelete = vi.fn();
    render(<TaskHistory items={[ITEMS[1]]} selectedId="b2" onSelect={vi.fn()} onDelete={nextDelete} />);
    await act(async () => {
      if (outcome === "success") resolve();
      else reject(new Error("old request failed"));
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("テスト指示2").closest(".history-item")).toHaveClass("selected");
    await userEvent.click(screen.getByRole("button", { name: "テスト指示2を削除" }));
    expect(nextDelete).toHaveBeenCalledExactlyOnceWith("b2");
    expect(onDelete).toHaveBeenCalledOnce();
  });

  it("keeps history readable during deletion and prevents duplicate delete requests", async () => {
    let finish!: () => void;
    const onDelete = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    const onSelect = vi.fn();
    render(<TaskHistory items={ITEMS} selectedId="a1" onSelect={onSelect} onDelete={onDelete} />);
    const remove = screen.getByRole("button", { name: "テスト指示1を削除" });
    act(() => { fireEvent.click(remove); fireEvent.click(remove); });
    expect(onDelete).toHaveBeenCalledExactlyOnceWith("a1");
    expect(remove).toBeDisabled();
    await userEvent.click(screen.getByTitle("テスト指示2"));
    expect(onSelect).toHaveBeenCalledExactlyOnceWith("b2");
    await act(async () => finish());
    await vi.waitFor(() => expect(remove).toBeEnabled());
  });

  it("keeps a legacy record readable and deletes it without selecting it", async () => {
    const onSelect = vi.fn();
    const onDelete = vi.fn();
    render(<TaskHistory items={[{ ...ITEMS[0], instruction: "", updated_at: 0, decision_calls: 1 }]}
      selectedId="a1" onSelect={onSelect} onDelete={onDelete} />);
    expect(screen.getByText("(指示なし)")).toBeVisible();
    expect(screen.getByText(/--:-- 決定1件・退避0件/)).toBeVisible();
    expect(screen.getByText("(指示なし)").closest(".history-item")).toHaveClass("selected");
    await userEvent.click(screen.getByRole("button", { name: "a1を削除" }));
    expect(onDelete).toHaveBeenCalledExactlyOnceWith("a1");
    expect(onSelect).not.toHaveBeenCalled();
  });
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
