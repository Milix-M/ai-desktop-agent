import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CollapsibleSection from "@/components/CollapsibleSection";

describe("CollapsibleSection", () => {
  it("starts collapsed by default", () => {
    render(
      <CollapsibleSection title="デバッグ">
        <div>中身</div>
      </CollapsibleSection>
    );
    expect(screen.queryByText("中身")).not.toBeInTheDocument();
  });

  it("starts open when defaultOpen", () => {
    render(
      <CollapsibleSection title="デバッグ" defaultOpen>
        <div>中身</div>
      </CollapsibleSection>
    );
    expect(screen.getByText("中身")).toBeInTheDocument();
  });

  it("toggles on click", async () => {
    render(
      <CollapsibleSection title="デバッグ">
        <div>中身</div>
      </CollapsibleSection>
    );
    await userEvent.click(screen.getByRole("button", { name: /デバッグ/ }));
    expect(screen.getByText("中身")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /デバッグ/ }));
    expect(screen.queryByText("中身")).not.toBeInTheDocument();
  });
});
