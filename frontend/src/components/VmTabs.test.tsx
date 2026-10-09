import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import VmTabs from "@/components/VmTabs";
import type { VmInfo } from "@/lib/types";

const VMS: VmInfo[] = [
  { id: "vm", name: "vm", status: "running", health: "healthy", vnc_port: 5900, ws_port: 6080, vnc_host: "vm", managed: false },
  { id: "vm-ab12", name: "work", status: "running", health: "starting", vnc_port: 5910, ws_port: 6090, vnc_host: "x", managed: true },
];

describe("VmTabs", () => {
  it("renders nothing useful when empty except add button", () => {
    render(
      <VmTabs vms={[]} selectedId={null} onSelect={vi.fn()} onCreate={vi.fn()} onDelete={vi.fn()} creating={false} />
    );
    expect(screen.getByText("+ VM追加")).toBeInTheDocument();
  });

  it("selects a vm on click", async () => {
    const onSelect = vi.fn();
    render(
      <VmTabs vms={VMS} selectedId="vm" onSelect={onSelect} onCreate={vi.fn()} onDelete={vi.fn()} creating={false} />
    );
    expect(screen.getByText("work")).toBeInTheDocument();
    await userEvent.click(screen.getByText("work"));
    expect(onSelect).toHaveBeenCalledWith("vm-ab12");
  });

  it("shows delete only for managed vms", async () => {
    const onDelete = vi.fn();
    render(
      <VmTabs vms={VMS} selectedId="vm" onSelect={vi.fn()} onCreate={vi.fn()} onDelete={onDelete} creating={false} />
    );
    expect(screen.queryByRole("button", { name: "vmを削除" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "workを削除" }));
    expect(onDelete).toHaveBeenCalledWith("vm-ab12");
  });

  it("creates qemu and container kinds", async () => {
    const onCreate = vi.fn();
    render(
      <VmTabs vms={[]} selectedId={null} onSelect={vi.fn()} onCreate={onCreate} onDelete={vi.fn()} creating={false} />
    );
    await userEvent.click(screen.getByText("+ コンテナ追加"));
    expect(onCreate).toHaveBeenCalledWith("container");
    await userEvent.click(screen.getByText("+ VM追加"));
    expect(onCreate).toHaveBeenCalledWith("qemu");
  });

  it("shows container badge", () => {
    const vms: VmInfo[] = [
      { id: "desk-ab12", name: "c1", status: "running", health: null, vnc_port: 5911, ws_port: 6091, vnc_host: "x", managed: true, kind: "container" },
    ];
    render(
      <VmTabs vms={vms} selectedId={null} onSelect={vi.fn()} onCreate={vi.fn()} onDelete={vi.fn()} creating={false} />
    );
    expect(screen.getByText(/コンテナ・/)).toBeInTheDocument();
  });

  it("disables adds while creating", () => {
    render(
      <VmTabs vms={VMS} selectedId="vm" onSelect={vi.fn()} onCreate={vi.fn()} onDelete={vi.fn()} creating={true} />
    );
    expect(screen.getAllByRole("button", { name: "作成中..." })).toHaveLength(2);
  });
});
