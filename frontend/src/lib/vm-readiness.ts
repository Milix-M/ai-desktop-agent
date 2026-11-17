export function isVmReady(vm: { status: string; health?: string | null } | null | undefined): boolean {
  return vm?.status === "running" && (vm.health == null || vm.health === "none" || vm.health === "healthy");
}

export function canStopVm(vm: { status: string }): boolean {
  return ["running", "restarting"].includes(vm.status);
}

export function vmStatusLabel(vm: { status: string; health?: string | null }): string {
  if (vm.status === "running") {
    if (isVmReady(vm)) return "起動中";
    if (vm.health === "starting") return "準備中";
    if (vm.health === "unhealthy") return "接続異常";
    return "状態不明";
  }
  return ({ creating: "準備中", created: "停止中", exited: "停止中", restarting: "再試行中",
    paused: "一時停止中", dead: "停止異常", removing: "削除中", not_found: "見つかりません" } as Record<string, string>)[vm.status] ?? "状態不明";
}
