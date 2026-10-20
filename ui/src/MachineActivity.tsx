import type { Machine, RpcCluster } from "./types";
import { downloadPercent, formatBytes, machineState, replicaReady, residentModels, transferBytes, transportLabel } from "./machine-state";

const muted = { color: "var(--muted)", fontSize: 11, marginTop: 4 };
const modelStyle = { color: "var(--accent)", fontFamily: "var(--mono)", fontSize: 12, overflowWrap: "anywhere" as const };

export function MachineStatus({ machine, cluster }: { machine: Machine; cluster?: RpcCluster }) {
  const state = machineState(machine, cluster);
  const color = state === "Offline" || state === "Error" ? "var(--red)"
    : state === "Serving" ? "var(--accent)"
    : state === "Ready" || state === "RPC worker" ? "var(--green)" : "var(--yellow)";
  return <span style={{ display: "inline-flex", whiteSpace: "nowrap", borderRadius: 99,
    padding: "3px 10px", fontSize: 11, fontWeight: 700, color,
    border: "1px solid currentColor", background: "rgba(255,255,255,.03)" }}>{state}</span>;
}

export function MachineModels({ machine: m, cluster }: { machine: Machine; cluster?: RpcCluster }) {
  const models = residentModels(m);
  const percent = downloadPercent(m);
  const downloading = (m.alive || m.pending) && m.phase === "DOWNLOADING";
  const bytes = transferBytes(m, cluster);
  const current = m.alive ? "Loaded" : "Last reported loaded";
  return <div style={{ minWidth: 200, maxWidth: 310 }}>
    {cluster ? <>
      <div style={modelStyle}>{cluster.model}</div>
      <div style={muted}>
        {!m.alive ? "Last reported replica" : replicaReady(cluster) ? current
          : cluster.status === "dissolving" ? "Releasing replica" : "Target · loading"}
        {" · "}{m.agent_id === cluster.primary_id ? "Primary" : "Worker"}
      </div>
      <div style={muted}>Replica {cluster.cluster_id.slice(0, 8)} · {transportLabel(cluster)}</div>
      {m.agent_id && cluster.preflight?.[m.agent_id] && <div style={muted}>
        Route check: {cluster.preflight[m.agent_id].replace(/_/g, " ")}
      </div>}
      {cluster.ctx_size != null && <div style={muted}>Context {cluster.ctx_size.toLocaleString()} tokens</div>}
      {bytes != null && !replicaReady(cluster) && cluster.status !== "dissolving" &&
        <div style={muted}>RPC data {m.agent_id === cluster.primary_id ? "sent" : "received"}: {formatBytes(bytes)}</div>}
    </> : m.in_rpc ? <div style={muted}>RPC assignment · awaiting model details</div>
      : models?.length ? <>
        <div style={muted}>{current} in Ollama</div>
        {models.map(name => <div style={modelStyle} key={name}>{name}</div>)}
      </> : <div style={muted}>{models === null ? "Loaded model state unavailable" : m.alive ? "No model loaded" : "No model loaded at last report"}</div>}
    {!cluster && m.model && <div style={muted}>Configured: {m.model}</div>}
    {downloading && <div style={{ marginTop: 8 }}>
      <div style={{ color: "var(--yellow)", fontSize: 11 }}>
        Downloading{percent == null ? " · progress not reported" : ` · ${percent}%`}
      </div>
      {percent != null && <progress aria-label={`Model download on ${m.hostname}`} value={percent} max={100}
        style={{ width: "100%", accentColor: "var(--yellow)", height: 7 }} />}
      <div style={{ ...muted, overflowWrap: "anywhere" }}>{m.detail}</div>
    </div>}
  </div>;
}

export function ReplicaOverview({ clusters, machines }: { clusters: RpcCluster[]; machines: Machine[] }) {
  if (!clusters.length) return null;
  function member(id: string) {
    const m = machines.find(m => m.agent_id === id);
    return m ? `${m.hostname}${m.alive ? "" : " · offline"}` : `Agent ${id.slice(0, 8)} · unavailable`;
  }
  return <section aria-label="Distributed model replicas" style={{ marginBottom: 20 }}>
    <h2 style={{ fontSize: 14, marginBottom: 10 }}>Distributed model replicas</h2>
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(290px, 1fr))", gap: 12 }}>
      {clusters.map(c => {
        const members = [c.primary_id, ...c.worker_ids];
        const healthy = members.every(id => machines.some(m => m.agent_id === id && m.alive && m.in_rpc));
        const ready = replicaReady(c) && healthy;
        const serving = ready && machines.some(m => m.agent_id === c.primary_id && m.busy);
        const state = c.status === "dissolving" ? "Releasing" : !healthy ? "Member unavailable"
          : ready ? serving ? "Serving" : "Ready" : "Loading model";
        return <article key={c.cluster_id} style={{ padding: 16, borderRadius: 12,
          border: "1px solid var(--border)", background: "var(--surface)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
            <span style={modelStyle}>{c.model}</span>
            <span style={{ fontSize: 11, color: ready ? "var(--green)" : "var(--yellow)" }}>{state}</span>
          </div>
          <div style={muted}>Replica {c.cluster_id.slice(0, 8)} · {transportLabel(c)}</div>
          {c.route_id && <div style={muted}>Route: {c.route_id}</div>}
          <div style={{ fontSize: 12, marginTop: 10 }}>Primary · {member(c.primary_id)}</div>
          {c.worker_ids.map(id => <div key={id} style={{ fontSize: 12 }}>Worker · {member(id)}</div>)}
          {c.ctx_size != null && <div style={muted}>Context {c.ctx_size.toLocaleString()} tokens</div>}
          {c.dissolve_reason && <div style={{ ...muted, color: "var(--yellow)" }}>Reason: {c.dissolve_reason.replace(/_/g, " ")}</div>}
        </article>;
      })}
    </div>
  </section>;
}
