import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import VMControls from "./VMControls";
import * as api from "@/lib/api";
import type { VmStatus } from "@/lib/types";

vi.mock("@/lib/api", async original => ({
  ...await original<typeof import("@/lib/api")>(), getVmStatus: vi.fn(), restartVm: vi.fn(),
}));

const healthy: VmStatus = { running: true, status: "running", health: "healthy", name: "作業環境" };
const starting: VmStatus = { ...healthy, health: "starting" };

beforeEach(() => { vi.resetAllMocks(); vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

test("a log subscriber reentering the restart control cannot submit a second command", async () => {
  vi.mocked(api.getVmStatus).mockResolvedValue(healthy);
  let finish!: (value: VmStatus) => void;
  vi.mocked(api.restartVm).mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const log = vi.fn((message: string) => {
    if (message === "VM再起動開始") screen.getByRole("button", { name: "再起動する" }).click();
  });
  await act(async () => { render(<VMControls onLog={log} pollIntervalMs={60000} />); });
  fireEvent.click(screen.getByRole("button", { name: "VM再起動" }));
  fireEvent.click(screen.getByRole("button", { name: "再起動する" }));
  expect(api.restartVm).toHaveBeenCalledOnce();
  expect(log.mock.calls.filter(([message]) => message === "VM再起動開始")).toHaveLength(1);
  await act(async () => finish(starting));
  expect(log).toHaveBeenCalledWith("VM再起動指示OK: 作業環境", "state");
  expect(screen.getByRole("button", { name: "VM再起動" })).toBeEnabled();
});

test.each([healthy, { ...healthy, health: "unhealthy" }])(
  "a $health observation immediately before the tracking deadline keeps its confirmed result",
  async observed => {
    let finishRestart!: (value: VmStatus) => void;
    const command = new Promise<VmStatus>(resolve => { finishRestart = resolve; });
    vi.mocked(api.restartVm).mockReturnValueOnce(command).mockResolvedValue(starting);
    const probes: { resolve: (value: VmStatus) => void; signal: AbortSignal }[] = [];
    let initial = true;
    vi.mocked(api.getVmStatus).mockImplementation(signal => {
      if (initial) { initial = false; return Promise.resolve(healthy); }
      return new Promise((resolve, reject) => {
        probes.push({ resolve, signal: signal! });
        signal!.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    });
    const log = vi.fn();
    await act(async () => { render(<VMControls onLog={log} pollIntervalMs={60000} restartTimeoutMs={30} />); });
    expect(screen.getByTestId("vm-status")).toHaveTextContent("起動中");
    fireEvent.click(screen.getByRole("button", { name: "VM再起動" }));
    fireEvent.click(screen.getByRole("button", { name: "再起動する" }));
    expect(probes).toHaveLength(1);
    await act(async () => finishRestart(starting));
    expect(probes).toHaveLength(2);
    expect(probes[0].signal.aborted).toBe(true);

    await act(async () => {
      probes[1].resolve(observed);
      // Let the public API continuation confirm the result, then let the
      // registered timer run before React commits its passive-effect cleanup.
      await Promise.resolve();
      vi.advanceTimersByTime(30);
    });
    expect(screen.getByRole("button", { name: "VM再起動" })).toBeEnabled();
    expect(screen.getByTestId("vm-status")).toHaveTextContent(
      observed.health === "healthy" ? "起動中" : "再起動に失敗しました",
    );
    expect(log).not.toHaveBeenCalledWith(expect.stringContaining("完了を確認できません"), "error");
    if (observed.health === "unhealthy") {
      expect(log.mock.calls.filter(([, level]) => level === "error")).toHaveLength(1);
    }
    fireEvent.click(screen.getByRole("button", { name: "VM再起動" }));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "再起動する" })));
    expect(api.restartVm).toHaveBeenCalledTimes(2);
  },
);
