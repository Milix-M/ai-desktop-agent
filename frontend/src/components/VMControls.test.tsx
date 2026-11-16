import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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
});
