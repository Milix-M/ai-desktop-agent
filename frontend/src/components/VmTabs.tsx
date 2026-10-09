"use client";

import { useState } from "react";
import type { VmInfo } from "@/lib/types";

type Kind = "qemu" | "container" | "android";

interface Props {
  vms: VmInfo[];
  selectedId: string | null;
  onSelect: (vmId: string) => void;
  onCreate: (kind: Kind, name?: string) => void;
  onDelete: (vmId: string) => void;
  creating: boolean;
}

const KIND_LABEL: Record<Kind, string> = {
  qemu: "VM",
  container: "コンテナ",
  android: "Android",
};

export default function VmTabs({ vms, selectedId, onSelect, onCreate, onDelete, creating }: Props) {
  const [formKind, setFormKind] = useState<Kind | null>(null);
  const [name, setName] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  const submitCreate = () => {
    if (formKind === null || creating) return;
    onCreate(formKind, name.trim() || undefined);
    setFormKind(null);
    setName("");
  };

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
              {vm.kind === "container" ? "コンテナ・" : vm.kind === "android" ? "Android・" : ""}
              {vm.status}
              {vm.health ? ` (${vm.health})` : ""}
            </span>
          </button>
          {vm.managed &&
            (confirmDeleteId === vm.id ? (
              <span className="vm-tab-confirm">
                <button
                  className="vm-tab-confirm-yes"
                  aria-label={`${vm.name}の削除を実行`}
                  onClick={() => {
                    setConfirmDeleteId(null);
                    onDelete(vm.id);
                  }}
                >
                  削除する
                </button>
                <button
                  className="vm-tab-confirm-no"
                  aria-label="削除をやめる"
                  onClick={() => setConfirmDeleteId(null)}
                >
                  やめる
                </button>
              </span>
            ) : (
              <button
                className="vm-tab-delete"
                aria-label={`${vm.name}を削除`}
                onClick={() => setConfirmDeleteId(vm.id)}
              >
                ×
              </button>
            ))}
        </div>
      ))}
      {(["qemu", "container", "android"] as Kind[]).map((kind) => (
        <button
          key={kind}
          className="vm-tab-add"
          onClick={() => {
            setFormKind(kind);
            setConfirmDeleteId(null);
          }}
          disabled={creating}
        >
          {creating ? "作成中..." : `+ ${KIND_LABEL[kind]}追加`}
        </button>
      ))}
      {formKind !== null && (
        <div className="vm-create-form" data-testid="vm-create-form">
          <span className="vm-create-label">{KIND_LABEL[formKind]}名（空可）</span>
          <input
            className="vm-create-input"
            aria-label={`${KIND_LABEL[formKind]}名`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submitCreate();
              if (e.key === "Escape") {
                setFormKind(null);
                setName("");
              }
            }}
            placeholder="空可"
            disabled={creating}
            autoFocus
          />
          <button
            className="vm-create-submit"
            onClick={submitCreate}
            disabled={creating}
          >
            作成する
          </button>
          <button
            className="vm-create-cancel"
            aria-label="作成をやめる"
            onClick={() => {
              setFormKind(null);
              setName("");
            }}
            disabled={creating}
          >
            やめる
          </button>
        </div>
      )}
    </div>
  );
}
