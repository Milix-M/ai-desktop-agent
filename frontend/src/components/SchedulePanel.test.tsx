import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import SchedulePanel from "./SchedulePanel";

afterEach(() => vi.unstubAllGlobals());

it("keeps the VM, local time and retry identity through submission and deletion", async () => {
  const submissions: Record<string, unknown>[] = [];
  const rows: object[] = [];
  let reads = 0;
  let deletes = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    if (init.method === "POST") {
      const payload = JSON.parse(String(init.body));
      submissions.push(payload);
      if (submissions.length === 1) throw new Error("受付結果不明");
      const row = { ...payload, id: payload.request_id, next_run: payload.run_at, last_error: null, last_task_id: null };
      rows.push(row);
      return Response.json(row);
    }
    if (init.method === "DELETE") {
      expect(url).toMatch(new RegExp("/schedules/" + submissions[0].request_id + "$"));
      if (++deletes === 1) return Response.json({ detail: "削除失敗" }, { status: 503 });
      rows.pop();
      return Response.json({ status: "deleted" });
    }
    if (++reads === 1) throw new Error("読み込み失敗");
    return Response.json(rows);
  }));
  const view = render(<SchedulePanel vmId={null} onSelectTask={vi.fn()} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("読み込み失敗");
  const form = screen.getByRole("button", { name: "予約する" }).closest("form")!;
  fireEvent.submit(form);
  view.rerender(<SchedulePanel vmId="desktop-2" onSelectTask={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("実行環境"), { target: { value: "existing" } });
  fireEvent.change(screen.getByLabelText("指示"), { target: { value: " 件数を確認 " } });
  const date = screen.getByLabelText(/開始日時/);
  fireEvent.change(date, { target: { value: "2000-01-01T09:30" } });
  fireEvent.submit(form);
  expect(screen.getByText("未来の日時を指定してください")).toBeVisible();
  fireEvent.change(date, { target: { value: "2099-03-10T09:30" } });
  fireEvent.change(screen.getByLabelText("繰り返し"), { target: { value: "604800" } });
  fireEvent.submit(form);
  expect(await screen.findByText(/受付結果不明/)).toBeVisible();
  fireEvent.submit(form);
  expect(await screen.findByText("件数を確認")).toBeVisible();
  expect(submissions).toHaveLength(2);
  expect(submissions[1]).toEqual(submissions[0]);
  expect(submissions[0]).toMatchObject({
    vm_id: "desktop-2", run_at: new Date("2099-03-10T09:30").getTime() / 1000,
    interval: 604800, instruction: "件数を確認",
  });
  const remove = await screen.findByRole("button", { name: "予約を削除" });
  fireEvent.click(remove);
  expect(await screen.findByText(/削除失敗/)).toBeVisible();
  fireEvent.click(remove);
  await waitFor(() => expect(remove).not.toBeInTheDocument());
});

it("shows the last dispatch outcome after reopening schedules", async () => {
  const row = { id: "one", vm_id: "desk", instruction: "画面確認", run_at: 100, interval: 0, next_run: null };
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([
    { ...row, last_task_id: "task-1", last_error: null },
    { ...row, id: "two", last_task_id: null, last_error: "この環境で別のタスクを実行中です" },
  ])));
  const select = vi.fn();
  render(<SchedulePanel vmId="desk" onSelectTask={select} />);
  fireEvent.click(await screen.findByRole("button", { name: "前回のタスクを見る" }));
  expect(select).toHaveBeenCalledWith("task-1");
  expect(screen.getByText(/開始失敗 · この環境で別のタスクを実行中です/)).toBeVisible();
});

it("rejects a mismatched registration receipt", async () => {
  const { addSchedule } = await import("@/lib/api");
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
  await expect(addSchedule("desktop", "画面確認", 100, 0, "expected")).rejects.toThrow("受付結果");
});

it.each([["container", "コンテナ"], ["qemu", "QEMU VM"], ["android", "Android"]])(
  "schedules a new %s without a selected environment", async (kind, label) => {
    const rows: object[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      if (init.method !== "POST") return Response.json(rows);
      const body = JSON.parse(String(init.body));
      expect(body).toMatchObject({ vm_id: null, vm_kind: kind });
      const row = { ...body, id: body.request_id, next_run: body.run_at, preparing: true, last_error: null, last_task_id: null };
      rows.push(row);
      return Response.json(row);
    }));
    render(<SchedulePanel vmId={null} onSelectTask={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("実行環境"), { target: { value: kind } });
    fireEvent.change(screen.getByLabelText("指示"), { target: { value: "集計" } });
    fireEvent.change(screen.getByLabelText(/開始日時/), { target: { value: "2099-03-10T09:30" } });
    fireEvent.click(screen.getByRole("button", { name: "予約する" }));
    expect(await screen.findByText(label + "（使い捨て） · 1回のみ")).toBeVisible();
    expect(screen.getByText(/^環境を準備中/)).toBeVisible();
  },
);
