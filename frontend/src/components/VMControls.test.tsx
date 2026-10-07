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

function stubConfirm(value: boolean) {
  (window as unknown as { confirm: (msg: string) => boolean }).confirm = vi
    .fn()
    .mockReturnValue(value);
}

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
    stubConfirm(true);
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

  it("restarts VM on button click", async () => {
    const onLog = vi.fn();
    vi.mocked(api.restartVm).mockResolvedValue({
      running: true,
      status: "restarting",
      health: null,
      name: "vm-1",
    });
    render(<VMControls onLog={onLog} pollIntervalMs={60000} />);

    await userEvent.click(screen.getByRole("button", { name: "VM作り直し" }));

    await waitFor(() => {
      expect(api.restartVm).toHaveBeenCalled();
    });
    expect(onLog).toHaveBeenCalledWith("VM作り直し開始", "action");
  });

  it("tracks restart until healthy", async () => {
    let calls = 0;
    vi.mocked(api.getVmStatus).mockImplementation(async () => {
      calls += 1;
      if (calls <= 3) {
        return { running: true, status: "restarting", health: "starting", name: "vm-1" };
      }
      return { running: true, status: "running", health: "healthy", name: "vm-1" };
    });
    let resolveRestart!: (v: import("@/lib/types").VmStatus) => void;
    vi.mocked(api.restartVm).mockImplementation(
      () => new Promise((resolve) => { resolveRestart = resolve; })
    );
    render(<VMControls pollIntervalMs={10} />);

    await userEvent.click(screen.getByRole("button", { name: "VM作り直し" }));
    // 指示応答待ちの間は作り直し中表示
    await waitFor(() => {
      expect(screen.getByTestId("vm-status")).toHaveTextContent("作り直し中");
    });
    resolveRestart({ running: true, status: "restarting", health: null, name: "vm-1" });
    // healthy 検出で通常表示に戻ること
    await waitFor(() => {
      expect(screen.getByTestId("vm-status")).toHaveTextContent("起動中");
    });
  });

  it("does nothing when confirm is cancelled", async () => {
    stubConfirm(false);
    render(<VMControls pollIntervalMs={60000} />);

    await userEvent.click(screen.getByRole("button", { name: "VM作り直し" }));
    expect(api.restartVm).not.toHaveBeenCalled();
  });
});
