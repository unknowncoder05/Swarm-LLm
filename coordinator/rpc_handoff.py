"""One-use, short-lived handoff of idle direct QUIC clusters across deployment.

Only metadata is restored. Native GPU processes and QUIC streams stay on agents.
HTTP relay buffers and in-flight jobs cannot be handed off by this mechanism.
"""
import json
from pathlib import Path
import re
import time
try:
    from rpc_performance import validate_runtime, DEFAULT_RUNTIME
except ModuleNotFoundError:
    from coordinator.rpc_performance import validate_runtime, DEFAULT_RUNTIME


def restore_quic_handoff(path, machines, clusters, assignments, factory, now=None):
    path = Path(path)
    if not path.exists():
        return []
    now = time.time() if now is None else now
    try:
        if path.is_symlink() or path.stat().st_size > 1024 * 1024 or path.stat().st_mode & 0o077:
            raise ValueError("Unsafe QUIC handoff file")
        data = json.loads(path.read_text())
        if data.get("version") != 1 or not -5 <= now - data["created_at"] <= 30:
            raise ValueError("Expired QUIC handoff")
        items = data["clusters"]
        if not isinstance(items, list) or len(items) > 4:
            raise ValueError("Invalid QUIC handoff cluster count")
        prepared = []; used_members = set(); used_clusters = set()
        for item in items:
            state = item["cluster"]
            cid = state["cluster_id"]
            members = [state["primary_id"], *state["worker_ids"]]
            if (state["transport"] != "quic" or state["status"] != "active" or not state["primary_ready"]
                    or state.get("relay_url") or len(members) != 2 or len(set(members)) != 2
                    or set(members) & used_members or cid in used_clusters or cid in clusters
                    or type(state["ctx_size"]) is not int or not 512 <= state["ctx_size"] <= 32768):
                raise ValueError("Invalid or overlapping QUIC cluster handoff")
            restored_assignments = {}; restored_machines = {}
            tokens = set()
            for identity in members:
                machine = machines.get(identity)
                record = item["machines"][identity]
                assignment = item["assignments"][identity]
                role = "primary" if identity == state["primary_id"] else "worker"
                endpoint = assignment.get("quic_endpoint", {})
                runtime = record.get("runtime") or {}
                if (not machine or identity in assignments or assignment.get("cluster_id") != cid
                        or assignment.get("transport") != "quic" or assignment.get("model") != state["model"]
                        or assignment.get("role") != role or assignment.get("dissolve")
                        or assignment.get("lan_ip") != machine.lan_ip or record["lan_ip"] != machine.lan_ip
                        or not 0 <= now - record["last_heartbeat"] <= 30
                        or not runtime.get("rpc_pid") or not runtime.get("agent_pid")
                        or not re.fullmatch(r"[a-f0-9]{64}", assignment.get("quic_token", ""))
                        or endpoint.get("agent_id") != identity or endpoint.get("ip") != machine.lan_ip
                        or type(endpoint.get("port")) is not int or not 1 <= endpoint["port"] <= 65535):
                    raise ValueError("QUIC handoff does not match registered agents")
                tokens.add(assignment["quic_token"])
                restored_assignments[identity] = assignment
                restored_machines[identity] = record
            if len(tokens) != 1:
                raise ValueError("QUIC handoff pair tokens differ")
            cluster = factory(cid, state["model"], state["primary_id"], state["worker_ids"],
                              transport="quic", ctx_size=state["ctx_size"])
            cluster.status = "active"; cluster.primary_ready = True
            cluster.formed_at = state["formed_at"]; cluster.last_job_at = state["last_job_at"]
            cluster.quic_stats = state.get("quic_stats", {})
            cluster.automatic = bool(state.get("automatic", False))
            cluster.route_id = state.get("route_id", "lan-quic")
            cluster.preflight = state.get("preflight", {})
            cluster.runtime_profile = validate_runtime(state.get("runtime_profile", {}))
            primary_profile = restored_assignments[state["primary_id"]].get("runtime_profile")
            if primary_profile is None and cluster.runtime_profile != DEFAULT_RUNTIME:
                raise ValueError("QUIC handoff omits its nondefault native settings")
            if primary_profile is not None and validate_runtime(primary_profile) != cluster.runtime_profile:
                raise ValueError("QUIC handoff runtime settings differ")
            prepared.append((cluster, restored_assignments, restored_machines))
            used_members.update(members); used_clusters.add(cid)
        # Validate everything before changing any reservation or assignment.
        for cluster, restored_assignments, records in prepared:
            clusters[cluster.cluster_id] = cluster
            assignments.update(restored_assignments)
            for identity, record in records.items():
                machine = machines[identity]
                machine.in_rpc = True; machine.rpc_cluster_id = cluster.cluster_id
                machine.alive = True
                machine.last_heartbeat = record["last_heartbeat"]
                machine.runtime = record["runtime"]
                machine.phase = "RPC_PRIMARY" if identity == cluster.primary_id else "RPC_WORKER"
        return [cluster.cluster_id for cluster, _, _ in prepared]
    finally:
        path.unlink(missing_ok=True)
