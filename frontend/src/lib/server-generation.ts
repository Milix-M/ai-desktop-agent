export class ServerGeneration {
  serverId: string | null = null;
  version = 0;
  private observed = false;
  private retired = new Set<string>();

  accept(serverId?: string, startedAt?: number): "accepted" | "reset" | "ignored" {
    if (serverId && serverId === this.serverId) return "accepted";
    if ((!serverId && this.serverId) || (serverId && this.retired.has(serverId))) return "ignored";
    if (startedAt !== undefined && startedAt !== this.version) return "ignored";
    if (!serverId) {
      this.observed = true;
      return "accepted";
    }
    const reset = this.observed;
    if (this.serverId) this.retired.add(this.serverId);
    this.serverId = serverId;
    this.version++;
    this.observed = true;
    return reset ? "reset" : "accepted";
  }
}
