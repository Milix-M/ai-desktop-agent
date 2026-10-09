import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import VncViewer from "@/components/VncViewer";

const listeners: Record<string, ((e?: unknown) => void)[]> = {};
const instances: unknown[] = [];

class MockRFB {
  fbWidth = 1280;
  fbHeight = 800;
  viewOnly = false;
  scaleViewport = false;
  resizeSession = true;
  connected = false;
  constructor(
    public target: unknown,
    public url: string,
    public opts: unknown
  ) {
    instances.push(this);
  }
  addEventListener(type: string, cb: (e?: unknown) => void) {
    listeners[type] = [...(listeners[type] ?? []), cb];
  }
  connect() {
    this.connected = true;
  }
  disconnect() {
    this.connected = false;
  }
}

vi.mock("@novnc/novnc", () => ({ default: MockRFB }));

function fire(type: string, event?: unknown) {
  for (const cb of listeners[type] ?? []) cb(event);
}

describe("VncViewer", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    for (const k of Object.keys(listeners)) delete listeners[k];
    instances.length = 0;
  });

  it("creates a connection on mount with given url", async () => {
    render(<VncViewer wsUrl="ws://example:6090" />);
    await waitFor(() => {
      expect(instances.length).toBeGreaterThan(0);
    });
    const first = instances[0] as MockRFB;
    expect(first.url).toBe("ws://example:6090");
  });

  it("notifies connect with resolution", async () => {
    const onChange = vi.fn();
    render(<VncViewer onConnectionChange={onChange} />);

    await waitFor(() => {
      expect(instances.length).toBeGreaterThan(0);
    });
    await act(async () => {
      fire("connect");
    });
    expect(onChange).toHaveBeenCalledWith(true, "1280x800");
  });

  it("notifies disconnect", async () => {
    const onChange = vi.fn();
    render(<VncViewer onConnectionChange={onChange} />);

    await waitFor(() => {
      expect(instances.length).toBeGreaterThan(0);
    });
    await act(async () => {
      fire("connect");
    });
    await act(async () => {
      fire("disconnect", { detail: { clean: false } });
    });
    expect(onChange).toHaveBeenCalledWith(false);
  });

  it("renders no overlay controls", async () => {
    const { container } = render(<VncViewer />);
    await waitFor(() => {
      expect(instances.length).toBeGreaterThan(0);
    });
    expect(container.querySelector(".vnc-screen")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
