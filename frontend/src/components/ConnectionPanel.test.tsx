import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import ConnectionPanel from "@/components/ConnectionPanel";

describe("ConnectionPanel", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true } as Response);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows backend, vnc and vm status", async () => {
    render(<ConnectionPanel vncConnected={true} vmResolution="1280x800" />);
    await waitFor(() => {
      expect(screen.getByText("バックエンド 接続中")).toBeInTheDocument();
    });
    expect(screen.getByText("VNC 接続中")).toBeInTheDocument();
    expect(screen.getByText("VM Desktop 1280x800")).toBeInTheDocument();
  });

  it("shows unconnected VM label when VNC is down", async () => {
    render(<ConnectionPanel vncConnected={false} />);
    await waitFor(() => {
      expect(screen.getByText("VM 未接続")).toBeInTheDocument();
    });
  });

  it("shows backend down on fetch failure", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("down"));
    render(<ConnectionPanel vncConnected={false} />);
    await waitFor(() => {
      expect(screen.getByText("バックエンド 未接続")).toBeInTheDocument();
    });
  });
});
