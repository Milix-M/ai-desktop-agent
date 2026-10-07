import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

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

import VncViewer from "@/components/VncViewer";

function fire(type: string, event?: unknown) {
  for (const cb of listeners[type] ?? []) cb(event);
}

describe("VncViewer", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    for (const k of Object.keys(listeners)) delete listeners[k];
    instances.length = 0;
  });

  it("shows reconnect button while disconnected", async () => {
    render(<VncViewer />);
    await waitFor(() => {
      expect(screen.getByTestId("vnc-reconnect")).toBeInTheDocument();
    });
  });

  it("hides reconnect button when connected, shows on dirty disconnect", async () => {
    const onChange = vi.fn();
    render(<VncViewer onConnectionChange={onChange} />);

    await waitFor(() => {
      expect(instances.length).toBeGreaterThan(0);
    });
    await act(async () => {
      fire("connect");
    });
    expect(onChange).toHaveBeenCalledWith(true, "1280x800");
    expect(screen.queryByTestId("vnc-reconnect")).not.toBeInTheDocument();

    await act(async () => {
      fire("disconnect", { detail: { clean: false } });
    });
    expect(screen.getByTestId("vnc-reconnect")).toBeInTheDocument();
  });

  it("manual reconnect creates a new connection", async () => {
    render(<VncViewer />);
    await waitFor(() => {
      expect(screen.getByTestId("vnc-reconnect")).toBeInTheDocument();
    });
    const before = instances.length;

    await userEvent.click(screen.getByTestId("vnc-reconnect"));
    await waitFor(() => {
      expect(instances.length).toBeGreaterThan(before);
    });
  });
});
