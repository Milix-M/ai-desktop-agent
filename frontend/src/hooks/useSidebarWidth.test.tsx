import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import {
  useSidebarWidth,
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_MAX_WIDTH,
} from "@/hooks/useSidebarWidth";

function Harness() {
  const { width, onResizeStart, onResizeMove, onResizeEnd } = useSidebarWidth();
  return (
    <div>
      <div data-testid="w">{width}</div>
      <div
        data-testid="handle"
        onPointerDown={onResizeStart}
        onPointerMove={onResizeMove}
        onPointerUp={onResizeEnd}
      />
    </div>
  );
}

describe("useSidebarWidth", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("starts at default width", () => {
    render(<Harness />);
    expect(screen.getByTestId("w")).toHaveTextContent("380");
  });

  it("drags to resize within limits", () => {
    render(<Harness />);
    const handle = screen.getByTestId("handle");
    fireEvent.pointerDown(handle, { clientX: 1000, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: 900, pointerId: 1 });
    // 右パネルなので左へ動かすと広がる
    expect(screen.getByTestId("w")).toHaveTextContent("480");
    fireEvent.pointerUp(handle, { pointerId: 1 });
    expect(window.localStorage.getItem("sidebar-width")).toBe("480");
  });

  it("clamps to min and max", () => {
    render(<Harness />);
    const handle = screen.getByTestId("handle");
    fireEvent.pointerDown(handle, { clientX: 1000, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: 10000, pointerId: 1 });
    expect(screen.getByTestId("w")).toHaveTextContent(String(SIDEBAR_MIN_WIDTH));
    fireEvent.pointerMove(handle, { clientX: -10000, pointerId: 1 });
    expect(screen.getByTestId("w")).toHaveTextContent(String(SIDEBAR_MAX_WIDTH));
    fireEvent.pointerUp(handle, { pointerId: 1 });
  });

  it("restores saved width", () => {
    window.localStorage.setItem("sidebar-width", "500");
    render(<Harness />);
    expect(screen.getByTestId("w")).toHaveTextContent("500");
  });
});
