import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import {
  useSidebarWidth,
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_MAX_WIDTH,
} from "@/hooks/useSidebarWidth";

function Harness() {
  const { width, onResizeStart } = useSidebarWidth();
  return (
    <div>
      <div data-testid="w">{width}</div>
      <div data-testid="handle" onMouseDown={onResizeStart} />
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
    fireEvent.mouseDown(handle, { clientX: 1000 });
    fireEvent.mouseMove(window, { clientX: 900 });
    // 右パネルなので左へ動かすと広がる
    expect(screen.getByTestId("w")).toHaveTextContent("480");
    fireEvent.mouseUp(window);
    expect(window.localStorage.getItem("sidebar-width")).toBe("480");
  });

  it("clamps to min and max", () => {
    render(<Harness />);
    const handle = screen.getByTestId("handle");
    fireEvent.mouseDown(handle, { clientX: 1000 });
    fireEvent.mouseMove(window, { clientX: 10000 });
    expect(screen.getByTestId("w")).toHaveTextContent(String(SIDEBAR_MIN_WIDTH));
    fireEvent.mouseMove(window, { clientX: -10000 });
    expect(screen.getByTestId("w")).toHaveTextContent(String(SIDEBAR_MAX_WIDTH));
    fireEvent.mouseUp(window);
  });

  it("restores saved width", () => {
    window.localStorage.setItem("sidebar-width", "500");
    render(<Harness />);
    expect(screen.getByTestId("w")).toHaveTextContent("500");
  });
});
