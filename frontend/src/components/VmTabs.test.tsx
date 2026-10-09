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
  it("renders nothing useful when empty except add buttons", () => {
    render(
      <VmTabs vms={[]} selectedId={null} onSelect={vi.fn()} onCreate={vi.fn()} onDelete={vi.fn()} creating={false} />
    );
    expect(screen.getByText("+ VM追加")).toBeInTheDocument();
    expect(screen.getByText("+ コンテナ追加")).toBeInTheDocument();
    expect(screen.getByText("+ Android追加")).toBeInTheDocument();
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
    render(
      <VmTabs vms={VMS} selectedId="vm" onSelect={vi.fn()} onCreate={vi.fn()} onDelete={vi.fn()} creating={false} />
    );
    expect(screen.queryByRole("button", { name: "vmを削除" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "workを削除" })).toBeInTheDocument();
  });

  it("asks confirmation before delete", async () => {
    const onDelete = vi.fn();
    render(
      <VmTabs vms={VMS} selectedId="vm" onSelect={vi.fn()} onCreate={vi.fn()} onDelete={onDelete} creating={false} />
    );
    await userEvent.click(screen.getByRole("button", { name: "workを削除" }));
    expect(onDelete).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "workの削除を実行" }));
    expect(onDelete).toHaveBeenCalledWith("vm-ab12");
  });

  it("cancels delete", async () => {
    const onDelete = vi.fn();
    render(
      <VmTabs vms={VMS} selectedId="vm" onSelect={vi.fn()} onCreate={vi.fn()} onDelete={onDelete} creating={false} />
    );
    await userEvent.click(screen.getByRole("button", { name: "workを削除" }));
    await userEvent.click(screen.getByRole("button", { name: "削除をやめる" }));
    expect(onDelete).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "workを削除" })).toBeInTheDocument();
  });

  it("creates with inline name form without popup", async () => {
    const onCreate = vi.fn();
    render(
      <VmTabs vms={[]} selectedId={null} onSelect={vi.fn()} onCreate={onCreate} onDelete={vi.fn()} creating={false} />
    );
    await userEvent.click(screen.getByText("+ コンテナ追加"));
    expect(onCreate).not.toHaveBeenCalled();
    await userEvent.type(screen.getByRole("textbox", { name: "コンテナ名" }), "c1");
    await userEvent.click(screen.getByRole("button", { name: "作成する" }));
    expect(onCreate).toHaveBeenCalledWith("container", "c1");
  });

  it("creates android kind", async () => {
    const onCreate = vi.fn();
    render(
      <VmTabs vms={[]} selectedId={null} onSelect={vi.fn()} onCreate={onCreate} onDelete={vi.fn()} creating={false} />
    );
    await userEvent.click(screen.getByText("+ Android追加"));
    await userEvent.click(screen.getByRole("button", { name: "作成する" }));
    expect(onCreate).toHaveBeenCalledWith("android", undefined);
  });

  it("shows container and android badges", () => {
    const vms: VmInfo[] = [
      { id: "desk-ab12", name: "c1", status: "running", health: null, vnc_port: 5911, ws_port: 6091, vnc_host: "x", managed: true, kind: "container" },
      { id: "and-ab12", name: "a1", status: "running", health: null, vnc_port: 5912, ws_port: 6092, vnc_host: "y", managed: true, kind: "android" },
    ];
    render(
      <VmTabs vms={vms} selectedId={null} onSelect={vi.fn()} onCreate={vi.fn()} onDelete={vi.fn()} creating={false} />
    );
    expect(screen.getByText(/コンテナ・/)).toBeInTheDocument();
    expect(screen.getByText(/Android・/)).toBeInTheDocument();
  });

  it("disables adds while creating", () => {
    render(
      <VmTabs vms={VMS} selectedId="vm" onSelect={vi.fn()} onCreate={vi.fn()} onDelete={vi.fn()} creating={true} />
    );
    expect(screen.getAllByRole("button", { name: "作成中..." })).toHaveLength(3);
  });
});
