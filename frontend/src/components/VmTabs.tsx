"use client";

import type { VmInfo } from "@/lib/types";

interface Props {
  vms: VmInfo[];
  selectedId: string | null;
  onSelect: (vmId: string) => void;
  onCreate: () => void;
  onDelete: (vmId: string) => void;
  creating: boolean;
}

export default function VmTabs({ vms, selectedId, onSelect, onCreate, onDelete, creating }: Props) {
  return (
    <div className="vm-tabs" data-testid="vm-tabs">
      {vms.map((vm) => (
        <div
          key={vm.id}
          className={`vm-tab${vm.id === selectedId ? " selected" : ""}`}
        >
          <button className="vm-tab-main" onClick={() => onSelect(vm.id)}>
            <span className={`vm-dot ${vm.status === "running" ? "green" : "red"}`} />
            <span className="vm-tab-name">{vm.name}</span>
            <span className="vm-tab-sub">
              {vm.status}
              {vm.health ? ` (${vm.health})` : ""}
            </span>
          </button>
          {vm.managed && (
            <button
              className="vm-tab-delete"
              aria-label={`${vm.name}を削除`}
              onClick={() => onDelete(vm.id)}
            >
              ×
            </button>
          )}
        </div>
      ))}
      <button
        className="vm-tab-add"
        onClick={onCreate}
        disabled={creating}
      >
        {creating ? "作成中..." : "+ VM追加"}
      </button>
    </div>
  );
}
