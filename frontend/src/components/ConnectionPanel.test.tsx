import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import ConnectionPanel from "@/components/ConnectionPanel";

describe("ConnectionPanel", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true } as Response);
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("shows backend, vnc and vm status", async () => {
    render(<ConnectionPanel statusConnected vncConnected={true} vmResolution="1280x800" />);
    await waitFor(() => {
      expect(screen.getByText("バックエンド 接続中")).toBeInTheDocument();
    });
    expect(screen.getByText("VNC 接続中")).toBeInTheDocument();
    expect(screen.getByText("VM Desktop 1280x800")).toBeInTheDocument();
  });

  it("requires both a healthy API and a synchronized state connection to show backend connected", async () => {
    const { rerender } = render(<ConnectionPanel statusConnected={false} vncConnected />);
    await act(async () => {});
    expect(screen.getByText("バックエンド 未接続")).toBeVisible();
    expect(screen.getByText("VNC 接続中")).toBeVisible();
    rerender(<ConnectionPanel statusConnected vncConnected />);
    expect(screen.getByText("バックエンド 接続中")).toBeVisible();
    rerender(<ConnectionPanel statusConnected={false} vncConnected />);
    expect(screen.getByText("バックエンド 未接続")).toBeVisible();
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("shows unconnected VM label when VNC is down", async () => {
    render(<ConnectionPanel statusConnected vncConnected={false} />);
    await waitFor(() => {
      expect(screen.getByText("VM 未接続")).toBeInTheDocument();
    });
  });

  it("shows backend down on fetch failure", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("down"));
    render(<ConnectionPanel statusConnected vncConnected={false} />);
    await waitFor(() => {
      expect(screen.getByText("バックエンド 未接続")).toBeInTheDocument();
    });
  });

  it("shows a connected desktop whose resolution has not arrived", async () => {
    render(<ConnectionPanel statusConnected vncConnected />);
    expect(screen.getByText("VM 取得中")).toBeVisible();
    await screen.findByText("バックエンド 接続中");
  });

  it("aborts a stalled health check, retries serially and recovers", async () => {
    vi.useFakeTimers();
    let pendingSignal: AbortSignal | undefined;
    vi.mocked(fetch).mockImplementationOnce((_url, options) => new Promise((_resolve, reject) => {
      pendingSignal = options?.signal ?? undefined;
      pendingSignal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    })).mockResolvedValue({ ok: true } as Response);
    const { unmount } = render(<ConnectionPanel statusConnected vncConnected={false} />);
    await act(() => vi.advanceTimersByTimeAsync(2999));
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(pendingSignal?.aborted).toBe(false);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(pendingSignal?.aborted).toBe(true);
    expect(screen.getByText("バックエンド 未接続")).toBeVisible();
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(screen.getByText("バックエンド 接続中")).toBeVisible();
    expect(vi.mocked(fetch).mock.calls[1][1]?.cache).toBe("no-store");
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("aborts in-flight checks on unmount and ignores a late successful response", async () => {
    vi.useFakeTimers();
    let finish!: (value: Response) => void;
    let signal: AbortSignal | null | undefined;
    vi.mocked(fetch).mockImplementation((_url, options) => {
      signal = options?.signal;
      return new Promise(resolve => { finish = resolve; });
    });
    const { unmount } = render(<ConnectionPanel statusConnected vncConnected={false} />);
    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => finish({ ok: true } as Response));
    await act(() => vi.advanceTimersByTimeAsync(20000));
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("cancels an in-flight failed check during unmount without scheduling another", async () => {
    vi.useFakeTimers();
    let fail!: (reason: Error) => void;
    vi.mocked(fetch).mockImplementation(() => new Promise((_resolve, reject) => { fail = reject; }));
    const { unmount } = render(<ConnectionPanel statusConnected vncConnected={false} />);
    unmount();
    await act(async () => fail(new TypeError("lost response")));
    expect(vi.getTimerCount()).toBe(0);
  });
});
