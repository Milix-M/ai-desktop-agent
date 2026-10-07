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

  it("does nothing when confirm is cancelled", async () => {
    stubConfirm(false);
    render(<VMControls pollIntervalMs={60000} />);

    await userEvent.click(screen.getByRole("button", { name: "VM作り直し" }));
    expect(api.restartVm).not.toHaveBeenCalled();
  });
});
