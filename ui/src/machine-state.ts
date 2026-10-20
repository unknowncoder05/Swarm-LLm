import type { Machine, RpcCluster } from "./types";

export function machineCluster(machine: Machine, clusters: RpcCluster[]): RpcCluster | undefined {
  // Membership IDs are authoritative. Hostnames can be reused by replacement agents.
  if (!machine.agent_id || !machine.in_rpc) return undefined;
  const id = machine.agent_id;
  return clusters.find(c => c.cluster_id === machine.rpc_cluster_id &&
    (c.primary_id === id || c.worker_ids.includes(id))) ??
    clusters.find(c => c.primary_id === id || c.worker_ids.includes(id));
}

export function replicaReady(cluster: RpcCluster): boolean {
  return cluster.status === "active" && cluster.primary_ready;
}

export function downloadPercent(machine: Machine): number | null {
  if (!machine.alive && !machine.pending) return null;
  if (machine.phase !== "DOWNLOADING") return null;
  const match = machine.detail?.match(/(?:^|\s)(\d+(?:\.\d+)?)\s*%/);
  if (!match) return null;
  const value = Number(match[1]);
  return Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
}

export function transferBytes(machine: Machine, cluster?: RpcCluster): number | null {
  if (!cluster || !machine.agent_id) return null;
  const stats = cluster.quic_stats?.[machine.agent_id];
  if (!stats) return null;
  return machine.agent_id === cluster.primary_id ? stats.sent_bytes : stats.received_bytes;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
}

export function transportLabel(cluster: RpcCluster): string {
  if (cluster.transport === "quic") return "Direct LAN · QUIC";
  if (cluster.transport === "direct") return "Direct TCP";
  // The API exposes a relay URL, but does not guarantee its geographic location.
  return cluster.relay_url ? "Relay" : "Coordinator relay";
}

export function machineState(machine: Machine, cluster?: RpcCluster): string {
  if (!machine.alive && !machine.pending) return "Offline";
  if (machine.phase === "ERROR") return "Error";
  if (machine.phase === "DOWNLOADING") return "Downloading";
  if (cluster) {
    if (cluster.status === "dissolving") return "Releasing";
    const checked = machine.agent_id ? cluster.preflight?.[machine.agent_id] : undefined;
    if (machine.phase === "RPC_CHECKING" && checked !== "verified" && checked !== "native_ready") return "Checking route";
    if (!replicaReady(cluster)) return "Loading model";
    return machine.busy ? "Serving" : machine.agent_id === cluster.primary_id ? "Ready" : "RPC worker";
  }
  if (machine.phase === "RPC_CHECKING") return "Checking route";
  if (machine.in_rpc) return "RPC reserved";
  if (machine.phase === "RPC_PREPARING") return "Preparing runtime";
  if (machine.pending) return machine.phase?.replace(/_/g, " ") ?? "Setting up";
  if (machine.runtime?.ollama_ready === false) return "Recovering";
  return machine.busy ? "Serving" : "Ready";
}

export function residentModels(machine: Machine): string[] | null {
  const models = machine.runtime?.ollama_models;
  return models == null ? null : models.flatMap(m => m.name ? [m.name] : []);
}
