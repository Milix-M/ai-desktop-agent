import { StrictMode } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act, cleanup, fireEvent } from "@testing-library/react";
import VncViewer from "@/components/VncViewer";

const instances: MockRFB[] = [];

// Only the public noVNC API: construction connects, disconnect() is terminal.
class MockRFB extends EventTarget {
  viewOnly = false;
  scaleViewport = false;
  resizeSession = true;
  disconnect = vi.fn();
  constructor(public target: HTMLElement, public url: string) {
    super();
    const canvas = document.createElement("canvas");
    canvas.width = 1280;
    canvas.height = 800;
    target.replaceChildren(canvas);
    instances.push(this);
  }
}

vi.mock("@novnc/novnc", () => ({ default: MockRFB }));

describe("VncViewer", () => {
  beforeEach(() => { instances.length = 0; });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("connects in view-only mode with the supplied url", async () => {
    render(<VncViewer wsUrl="ws://example:6090" />);
    await waitFor(() => expect(instances).toHaveLength(1));
    expect(instances[0].url).toBe("ws://example:6090");
    expect(instances[0].viewOnly).toBe(true);
    expect(instances[0].scaleViewport).toBe(true);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("accepts manual input only while allowed, without reconnecting the desktop", async () => {
    const { rerender } = render(<VncViewer allowInput={false} wsUrl="ws://desktop" />);
    await waitFor(() => expect(instances).toHaveLength(1));
    expect(instances[0].viewOnly).toBe(true);
    rerender(<VncViewer allowInput wsUrl="ws://desktop" />);
    expect(instances[0].viewOnly).toBe(false);
    rerender(<VncViewer allowInput={false} wsUrl="ws://desktop" />);
    expect(instances[0].viewOnly).toBe(true);
    expect(instances).toHaveLength(1);
    expect(instances[0].disconnect).not.toHaveBeenCalled();
  });

  it("keeps manual input enabled after a desktop reconnect", async () => {
    render(<VncViewer allowInput wsUrl="ws://desktop" />);
    await waitFor(() => expect(instances).toHaveLength(1));
    expect(instances[0].viewOnly).toBe(false);
    vi.useFakeTimers();
    act(() => instances[0].dispatchEvent(new Event("disconnect")));
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(instances[1].viewOnly).toBe(false);
  });

  it.each(["permission", "environment", "unmount", "disconnect"])("releases held input before retiring the viewer for %s", async mode => {
    const { rerender, unmount } = render(<VncViewer allowInput wsUrl="ws://first" />);
    await waitFor(() => expect(instances).toHaveLength(1));
    const first = instances[0];
    const canvas = first.target.querySelector("canvas")!;
    const releases: boolean[] = [];
    canvas.addEventListener("keyup", () => releases.push(first.viewOnly));
    window.addEventListener("mouseup", () => releases.push(first.viewOnly), { once: true });
    fireEvent.keyDown(canvas, { key: "Control", code: "ControlLeft" });
    fireEvent.mouseDown(canvas, { buttons: 1 });
    if (mode === "permission") rerender(<VncViewer allowInput={false} wsUrl="ws://first" />);
    else if (mode === "environment") rerender(<VncViewer allowInput wsUrl="ws://second" />);
    else if (mode === "unmount") unmount();
    else act(() => first.dispatchEvent(new Event("disconnect")));
    expect(releases).toEqual([false, false]);
    if (mode === "permission") {
      expect(first.viewOnly).toBe(true);
      expect(first.disconnect).not.toHaveBeenCalled();
    }
  });

  it("reports the canvas resolution on connection", async () => {
    const onChange = vi.fn();
    render(<VncViewer onConnectionChange={onChange} />);
    await waitFor(() => expect(instances).toHaveLength(1));
    act(() => instances[0].dispatchEvent(new Event("connect")));
    expect(onChange).toHaveBeenLastCalledWith(true, "1280x800");
  });

  it.each([false, true])("creates a new RFB after disconnect (clean=%s)", async (clean) => {
    const onChange = vi.fn();
    render(<VncViewer onConnectionChange={onChange} />);
    await waitFor(() => expect(instances).toHaveLength(1));
    vi.useFakeTimers();
    act(() => instances[0].dispatchEvent(new CustomEvent("disconnect", { detail: { clean } })));
    expect(onChange).toHaveBeenLastCalledWith(false);
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(instances).toHaveLength(2);
    expect(instances[1].url).toBe(instances[0].url);
  });

  it("cancels reconnects when unmounted", async () => {
    const { unmount } = render(<VncViewer />);
    await waitFor(() => expect(instances).toHaveLength(1));
    vi.useFakeTimers();
    act(() => instances[0].dispatchEvent(new Event("disconnect")));
    unmount();
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(instances).toHaveLength(1);
  });

  it("does not reconnect an old VM after switching tabs", async () => {
    const onChange = vi.fn();
    const { rerender } = render(<VncViewer wsUrl="ws://first" onConnectionChange={onChange} />);
    await waitFor(() => expect(instances).toHaveLength(1));
    const old = instances[0];
    rerender(<VncViewer wsUrl="ws://second" onConnectionChange={onChange} />);
    await waitFor(() => expect(instances).toHaveLength(2));
    expect(old.disconnect).toHaveBeenCalledOnce();
    onChange.mockClear();
    vi.useFakeTimers();
    act(() => old.dispatchEvent(new Event("connect")));
    act(() => old.dispatchEvent(new Event("disconnect")));
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(instances).toHaveLength(2);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("reports a connected screen while its first framebuffer size is still unknown", async () => {
    const onChange = vi.fn();
    render(<VncViewer onConnectionChange={onChange} />);
    await waitFor(() => expect(instances).toHaveLength(1));
    instances[0].target.replaceChildren();
    act(() => instances[0].dispatchEvent(new Event("connect")));
    expect(onChange).toHaveBeenLastCalledWith(true, undefined);
  });

  it("does not leak connections during StrictMode setup and cleanup", async () => {
    const { unmount } = render(<StrictMode><VncViewer /></StrictMode>);
    await waitFor(() => expect(instances).toHaveLength(1));
    unmount();
    expect(instances[0].disconnect).toHaveBeenCalledOnce();
  });
});
