import { describe, expect, it } from "vitest";
import { canStopVm, isVmReady, vmStatusLabel } from "./vm-readiness";

describe("environment readiness", () => {
  it.each([undefined, null])("does not accept a missing environment: %s", (vm) => {
    expect(isVmReady(vm)).toBe(false);
  });

  it.each([undefined, null, "none", "healthy"])("accepts a running desktop with health %s", (health) => {
    const vm = { status: "running", health };
    expect(isVmReady(vm)).toBe(true);
    expect(canStopVm(vm)).toBe(true);
    expect(vmStatusLabel(vm)).toBe("起動中");
  });

  it.each([
    ["starting", "準備中"],
    ["unhealthy", "接続異常"],
    ["unknown", "状態不明"],
  ])("allows stopping but rejects work while health is %s", (health, label) => {
    const vm = { status: "running", health };
    expect(isVmReady(vm)).toBe(false);
    expect(canStopVm(vm)).toBe(true);
    expect(vmStatusLabel(vm)).toBe(label);
  });

  it.each([
    ["creating", "準備中", false],
    ["created", "停止中", false],
    ["exited", "停止中", false],
    ["restarting", "再試行中", true],
    ["paused", "一時停止中", false],
    ["dead", "停止異常", false],
    ["removing", "削除中", false],
    ["not_found", "見つかりません", false],
    ["unknown", "状態不明", false],
  ] as const)("rejects work for %s even with a stale healthy status", (status, label, stoppable) => {
    const vm = { status, health: "healthy" };
    expect(isVmReady(vm)).toBe(false);
    expect(canStopVm(vm)).toBe(stoppable);
    expect(vmStatusLabel(vm)).toBe(label);
  });
});
