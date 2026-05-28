import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import InstructionInput from "@/components/InstructionInput";

describe("InstructionInput", () => {
  it("renders textarea and submit button", () => {
    render(<InstructionInput onSubmit={vi.fn()} disabled={false} />);

    expect(screen.getByPlaceholderText(/LibreOffice/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "実行" })).toBeInTheDocument();
  });

  it("disables submit button when textarea is empty", () => {
    render(<InstructionInput onSubmit={vi.fn()} disabled={false} />);
    expect(screen.getByRole("button")).toBeDisabled();
  });

  it("enables submit button when text is entered", async () => {
    render(<InstructionInput onSubmit={vi.fn()} disabled={false} />);

    await userEvent.type(
      screen.getByPlaceholderText(/LibreOffice/),
      "テスト指示"
    );

    expect(screen.getByRole("button")).not.toBeDisabled();
  });

  it("calls onSubmit and clears input on button click", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<InstructionInput onSubmit={onSubmit} disabled={false} />);

    const textarea = screen.getByPlaceholderText(/LibreOffice/);
    await userEvent.type(textarea, "テスト指示");
    await userEvent.click(screen.getByRole("button"));

    expect(onSubmit).toHaveBeenCalledWith("テスト指示");
    expect(textarea).toHaveValue("");
  });

  it("calls onSubmit on Enter key (without Shift)", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<InstructionInput onSubmit={onSubmit} disabled={false} />);

    const textarea = screen.getByPlaceholderText(/LibreOffice/);
    await userEvent.type(textarea, "テスト指示");
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

    expect(onSubmit).toHaveBeenCalledWith("テスト指示");
  });

  it("does NOT call onSubmit on Shift+Enter", async () => {
    const onSubmit = vi.fn();
    render(<InstructionInput onSubmit={onSubmit} disabled={false} />);

    const textarea = screen.getByPlaceholderText(/LibreOffice/);
    await userEvent.type(textarea, "テスト指示");
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });

    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("disables everything when disabled=true", () => {
    render(<InstructionInput onSubmit={vi.fn()} disabled={true} />);

    expect(screen.getByPlaceholderText(/LibreOffice/)).toBeDisabled();
    expect(screen.getByRole("button")).toBeDisabled();
    expect(screen.getByRole("button")).toHaveTextContent("処理中...");
  });
});
