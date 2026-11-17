import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import VMControls from "@/components/VMControls";
import * as api from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getVmStatus: vi.fn(),
    restartVm: vi.fn(),
  };
});

describe("VMControls", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getVmStatus).mockResolvedValue({
      running: true,
      status: "running",
      health: "healthy",
      name: "vm-1",
    });
    vi.mocked(api.restartVm).mockResolvedValue({
      running: true,
      status: "restarting",
      health: null,
      name: "vm-1",
    });
  });

  it("shows running status", async () => {
    render(<VMControls pollIntervalMs={60000} />);
    await waitFor(() => {
      expect(screen.getByTestId("vm-status")).toHaveTextContent("起動中");
    });
  });

  it("shows stopped status", async () => {
    vi.mocked(api.getVmStatus).mockResolvedValue({
      running: false,
      status: "exited",
      health: null,
      name: "vm-1",
    });
    render(<VMControls pollIntervalMs={60000} />);
    await waitFor(() => {
      expect(screen.getByTestId("vm-status")).toHaveTextContent("停止中");
    });
  });

  it("restarts VM after inline confirm", async () => {
    const onLog = vi.fn();
    vi.mocked(api.restartVm).mockResolvedValue({
      running: true,
      status: "restarting",
      health: null,
      name: "vm-1",
    });
    render(<VMControls onLog={onLog} pollIntervalMs={60000} />);

    await userEvent.click(screen.getByRole("button", { name: "VM再起動" }));
    // 1クリックでは実行されない
    expect(api.restartVm).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "再起動する" }));

    await waitFor(() => {
      expect(api.restartVm).toHaveBeenCalled();
    });
    expect(onLog).toHaveBeenCalledWith("VM再起動開始", "action");
  });

  it("tracks restart until healthy", async () => {
    let done = false;
    vi.mocked(api.getVmStatus).mockImplementation(async () => {
      if (done) {
        return { running: true, status: "running", health: "healthy", name: "vm-1" };
      }
      return { running: true, status: "restarting", health: "starting", name: "vm-1" };
    });
    let resolveRestart!: (v: import("@/lib/types").VmStatus) => void;
    vi.mocked(api.restartVm).mockImplementation(
      () => new Promise((resolve) => { resolveRestart = resolve; })
    );
    render(<VMControls pollIntervalMs={10} />);

    await userEvent.click(screen.getByRole("button", { name: "VM再起動" }));
    await userEvent.click(screen.getByRole("button", { name: "再起動する" }));
    // 指示応答待ちの間は再起動中表示
    await waitFor(() => {
      expect(screen.getByTestId("vm-status")).toHaveTextContent("再起動中");
    });
    done = true;
    resolveRestart({ running: true, status: "restarting", health: null, name: "vm-1" });
    // healthy 検出で通常表示に戻ること
    await waitFor(() => {
      expect(screen.getByTestId("vm-status")).toHaveTextContent("起動中");
    });
  });

  it("cancels restart", async () => {
    render(<VMControls pollIntervalMs={60000} />);

    await userEvent.click(screen.getByRole("button", { name: "VM再起動" }));
    await userEvent.click(screen.getByRole("button", { name: "やめる" }));
    expect(api.restartVm).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "VM再起動" })).toBeInTheDocument();
  });

  it.each([
    { status: "running", running: true, health: "unhealthy" },
    { status: "exited", running: false, health: null },
  ])("ends tracking when restart reaches an unusable state: $status/$health", async (failed) => {
    vi.mocked(api.getVmStatus).mockResolvedValue({ ...failed, name: "vm-1" });
    const log = vi.fn();
    render(<VMControls onLog={log} pollIntervalMs={60000} />);
    await userEvent.click(screen.getByRole("button", { name: "VM再起動" }));
    await userEvent.click(screen.getByRole("button", { name: "再起動する" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "VM再起動" })).toBeEnabled());
    expect(log).toHaveBeenCalledWith(expect.stringContaining("再起動に失敗"), "error");
  });

  it("ends tracking for a ready legacy image without a healthcheck", async () => {
    vi.mocked(api.getVmStatus).mockResolvedValue({ running: true, status: "running", health: null, name: "vm-1" });
    render(<VMControls pollIntervalMs={60000} />);
    await userEvent.click(screen.getByRole("button", { name: "VM再起動" }));
    await userEvent.click(screen.getByRole("button", { name: "再起動する" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "VM再起動" })).toBeEnabled());
    expect(screen.getByTestId("vm-status")).toHaveTextContent("起動中");
  });

  it("keeps tracking when a healthy response predates the restart request", async () => {
    let resolveOld!: (status: import("@/lib/types").VmStatus) => void;
    vi.mocked(api.getVmStatus).mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; }));
    vi.mocked(api.getVmStatus).mockResolvedValue({ running: true, status: "restarting", health: "starting", name: "vm-1" });
    vi.mocked(api.restartVm).mockReturnValue(new Promise(() => {}));
    render(<VMControls pollIntervalMs={60000} />);
    await userEvent.click(screen.getByRole("button", { name: "VM再起動" }));
    await userEvent.click(screen.getByRole("button", { name: "再起動する" }));
    await act(async () => resolveOld({ running: true, status: "running", health: "healthy", name: "vm-1" }));
    expect(screen.getByTestId("vm-status")).toHaveTextContent("再起動中");
  });

  it("bounds restart tracking even if the restart request never responds", async () => {
    vi.mocked(api.restartVm).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.getVmStatus).mockResolvedValue({ running: true, status: "restarting", health: "starting", name: "vm-1" });
    const log = vi.fn();
    render(<VMControls onLog={log} pollIntervalMs={60000} restartTimeoutMs={30} />);
    await userEvent.click(screen.getByRole("button", { name: "VM再起動" }));
    await userEvent.click(screen.getByRole("button", { name: "再起動する" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "VM再起動" })).toBeEnabled());
    expect(log).toHaveBeenCalledWith(expect.stringContaining("完了を確認できません"), "error");
  });

  it("aborts stalled status requests and polls without overlapping", async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    vi.mocked(api.getVmStatus).mockImplementation(signal => new Promise((_resolve, reject) => {
      signals.push(signal!);
      signal!.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
    }));
    try {
      const { unmount } = render(<VMControls pollIntervalMs={5} />);
      await act(async () => vi.advanceTimersByTimeAsync(9999));
      expect(api.getVmStatus).toHaveBeenCalledTimes(1);
      await act(async () => vi.advanceTimersByTimeAsync(1));
      expect(signals[0].aborted).toBe(true);
      await act(async () => vi.advanceTimersByTimeAsync(5));
      expect(api.getVmStatus).toHaveBeenCalledTimes(2);
      unmount();
      expect(signals[1].aborted).toBe(true);
    } finally { vi.useRealTimers(); }
  });

  it("ignores a late restart response after its tracking deadline", async () => {
    let resolveRestart!: (status: import("@/lib/types").VmStatus) => void;
    vi.mocked(api.restartVm).mockReturnValue(new Promise(resolve => { resolveRestart = resolve; }));
    const log = vi.fn();
    render(<VMControls onLog={log} pollIntervalMs={60000} restartTimeoutMs={30} />);
    await userEvent.click(screen.getByRole("button", { name: "VM再起動" }));
    await userEvent.click(screen.getByRole("button", { name: "再起動する" }));
    await waitFor(() => expect(log).toHaveBeenCalledWith(expect.stringContaining("完了を確認できません"), "error"));
    await act(async () => resolveRestart({ running: true, status: "running", health: "healthy", name: "late-command" }));
    expect(log).not.toHaveBeenCalledWith("VM再起動指示OK: late-command", "state");
  });

  it("ignores the restart response after the component unmounts", async () => {
    let resolveRestart!: (status: import("@/lib/types").VmStatus) => void;
    vi.mocked(api.restartVm).mockReturnValue(new Promise(resolve => { resolveRestart = resolve; }));
    const log = vi.fn();
    const { unmount } = render(<VMControls onLog={log} pollIntervalMs={60000} />);
    await userEvent.click(screen.getByRole("button", { name: "VM再起動" }));
    await userEvent.click(screen.getByRole("button", { name: "再起動する" }));
    unmount();
    const count = log.mock.calls.length;
    await act(async () => resolveRestart({ running: true, status: "running", health: "healthy", name: "retired" }));
    expect(log).toHaveBeenCalledTimes(count);
  });

  it("does not confirm from a poll started while the restart command was pending", async () => {
    let resolveRestart!: (status: import("@/lib/types").VmStatus) => void;
    let resolvePoll!: (status: import("@/lib/types").VmStatus) => void;
    const healthy = { running: true, status: "running", health: "healthy", name: "vm-1" };
    vi.mocked(api.getVmStatus).mockResolvedValueOnce(healthy)
      .mockReturnValueOnce(new Promise(resolve => { resolvePoll = resolve; }))
      .mockResolvedValue({ ...healthy, health: "starting" });
    vi.mocked(api.restartVm).mockReturnValue(new Promise(resolve => { resolveRestart = resolve; }));
    render(<VMControls pollIntervalMs={60000} />);
    await waitFor(() => expect(screen.getByTestId("vm-status")).toHaveTextContent("起動中"));
    await userEvent.click(screen.getByRole("button", { name: "VM再起動" }));
    await userEvent.click(screen.getByRole("button", { name: "再起動する" }));
    await waitFor(() => expect(api.getVmStatus).toHaveBeenCalledTimes(2));
    await act(async () => { resolveRestart(healthy); resolvePoll(healthy); });
    expect(screen.getByTestId("vm-status")).toHaveTextContent("再起動中");
    expect(screen.getByRole("button", { name: "VM再起動" })).toBeDisabled();
  });
});
