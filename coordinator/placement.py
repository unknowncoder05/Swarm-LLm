"""GPU eligibility and fair placement policy; no reservations or network calls."""

from itertools import combinations
import time
from typing import Optional

try:
    from models import MachineState
except ModuleNotFoundError:
    from coordinator.models import MachineState


def _physical_host(machine: MachineState):
    return (machine.ip, machine.lan_ip or machine.hostname)


class PlacementPolicy:
    def __init__(
        self, machines, rpc_models, single_models, queued_jobs, dead_after=120
    ):
        self.machines = machines
        self.rpc_models = rpc_models
        self.single_models = single_models
        self.queued_jobs = queued_jobs
        self.dead_after = dead_after

    def on_demand_capacity(self, model: str) -> list[MachineState]:
        required_vram = self.single_models.get(model)
        if required_vram is None:
            return []
        return [
            machine
            for machine in self.machines.values()
            if machine.alive
            and not machine.in_rpc
            and (machine.gpu_vram_gb or 0) >= required_vram + 1.0
        ]

    def worker_available(self, machine: MachineState) -> bool:
        """One coordinator-owned workload slot, shared by chat, media and containers."""
        return (
            machine.alive
            and not machine.busy
            and not machine.in_rpc
            and time.time() - getattr(machine, "last_heartbeat", time.time())
            <= self.dead_after
            and machine.media_phase not in ("RUNNING", "DOWNLOADING_DEPS")
            and getattr(machine, "phase", "RUNNING")
            not in ("DOWNLOADING", "STARTING", "REGISTERING", "ERROR")
            and not (machine.runtime or {}).get("rpc_pid")
            and not any(
                other is not machine
                and other.alive
                and _physical_host(other) == _physical_host(machine)
                and (other.busy or other.in_rpc or (other.runtime or {}).get("rpc_pid"))
                for other in self.machines.values()
            )
        )

    def single_context_limit(self, machine: MachineState, model: str) -> int:
        # Conservative scheduling policy, not the model's architectural maximum.
        # Reserve space for KV/compute rather than sizing from weights alone.
        policies = {
            "qwen3:8b": ((6, 2048), (8, 8192), (12, 32768)),
            "qwen3:14b": ((11, 4096), (12, 8192), (24, 32768)),
            "gemma4:12b": ((9, 2048), (12, 4096), (24, 16384)),
            "devstral-small-2:24b": ((16, 2048), (24, 16384)),
        }
        return max(
            (
                ctx
                for vram, ctx in policies.get(model, ())
                if (machine.gpu_vram_gb or 0) >= vram
            ),
            default=4096,
        )

    def can_serve_single(
        self, machine: MachineState, model: str, context: Optional[int] = None
    ) -> bool:
        if model in self.rpc_models:
            return False  # even a large GPU uses the cluster's native primary path
        required = self.single_models.get(model)
        if required is not None:
            fits = (machine.gpu_vram_gb or 0) >= required + 1.0
        else:
            fits = machine.model == model  # explicitly registered custom models
        return fits and (
            context is None or context <= self.single_context_limit(machine, model)
        )

    def select_rpc_machines(
        self, required_vram: float, use_regional: bool = False, agent_ids=None
    ):
        idle = sorted(
            [
                m
                for m in self.machines.values()
                if self.worker_available(m)
                and (agent_ids is None or m.agent_id in agent_ids)
                and (m.gpu_vram_gb or 0) > 0
                and (
                    not use_regional
                    or (m.gpu_vram_gb or 0) >= required_vram
                    or (m.runtime or {}).get("rpc_relay_external")
                )
            ],
            key=lambda m: m.gpu_vram_gb or 0,
            reverse=True,
        )
        # Reserve the smallest eligible devices for older single-machine demand,
        # so a new RPC request cannot consume all its still-unclaimed capacity.
        reserved = set()
        for job in sorted(self.queued_jobs(), key=lambda item: item.created_at):
            if job.model in self.rpc_models or job.rpc_cluster_id or job.rpc_recovering:
                continue
            candidates = [
                m
                for m in idle
                if _physical_host(m) not in reserved
                and self.can_serve_single(
                    m, job.model, job.body.get("options", {}).get("num_ctx")
                )
            ]
            if candidates:
                reserved.add(_physical_host(
                    min(
                        candidates, key=lambda m: (m.gpu_vram_gb or 0, m.agent_id)
                    )
                ))
        idle = [m for m in idle if _physical_host(m) not in reserved]
        unique_hosts = set()
        unique_idle = []
        for machine in idle:
            host = _physical_host(machine)
            if host not in unique_hosts:
                unique_hosts.add(host)
                unique_idle.append(machine)
        idle = unique_idle
        selected: list[MachineState] = []
        total = 0.0
        for m in idle:
            selected.append(m)
            total += m.gpu_vram_gb or 0
            if total >= required_vram:
                break
        if total < required_vram:
            return None
        return selected

    def automatic_pair(self, required_vram, agent_ids=None):
        eligible = [
            m
            for m in self.machines.values()
            if self.worker_available(m)
            and (m.runtime or {}).get("rpc_preflight")
            and (agent_ids is None or m.agent_id in agent_ids)
        ]
        pairs = []
        for a, b in combinations(eligible[:32], 2):
            if _physical_host(a) == _physical_host(b):
                continue
            ids = [a.agent_id, b.agent_id]
            selected = self.select_rpc_machines(required_vram, agent_ids=ids)
            if selected and len(selected) == 2:
                # Address affinity only orders probes; it never establishes reachability.
                affinity = bool(
                    a.lan_ip
                    and b.lan_ip
                    and a.lan_ip.rsplit(".", 1)[0] == b.lan_ip.rsplit(".", 1)[0]
                )
                pairs.append(
                    (
                        not affinity,
                        -sum(m.gpu_vram_gb or 0 for m in selected),
                        ids,
                        selected,
                    )
                )
        return min(pairs, key=lambda value: value[:2])[3] if pairs else None
