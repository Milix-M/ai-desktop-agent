import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import StatusBar from "@/components/StatusBar";

describe("StatusBar", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true } as Response);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows backend, vnc, vm and agent status", async () => {
    render(<StatusBar vncConnected={true} agentState="executing" vmResolution="1280x800" />);
    await waitFor(() => {
      expect(screen.getByText("Backend OK")).toBeInTheDocument();
    });
    expect(screen.getByText("VNC 接続中")).toBeInTheDocument();
    expect(screen.getByText("VM Desktop 1280x800")).toBeInTheDocument();
    expect(screen.getByText("Agent 実行中")).toBeInTheDocument();
  });

  it("shows raw resolution without special-casing", async () => {
    render(<StatusBar vncConnected={false} agentState="idle" vmResolution="720x400" />);
    await waitFor(() => {
      expect(screen.getByText("VM Desktop 720x400")).toBeInTheDocument();
    });
    expect(screen.getByText("VNC 未接続")).toBeInTheDocument();
  });

  it("labels interrupted state", async () => {
    render(<StatusBar vncConnected={true} agentState="interrupted" />);
    await waitFor(() => {
      expect(screen.getByText("Agent 中断")).toBeInTheDocument();
    });
  });

  it("shows backend down on fetch failure", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("down"));
    render(<StatusBar vncConnected={false} agentState="idle" />);
    await waitFor(() => {
      expect(screen.getByText("Backend DOWN")).toBeInTheDocument();
    });
  });
});
