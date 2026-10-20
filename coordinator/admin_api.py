"""Administrative inventory and usage routes with explicit runtime dependencies."""

import statistics
from typing import Optional
from fastapi import Depends


def install_admin_routes(
    app,
    *,
    require_admin,
    repository,
    machines,
    pending,
    rpc_clusters,
    active_chat_streams,
    queued_jobs,
):
    @app.get("/machines")
    async def get_machines(_key: str = Depends(require_admin)):
        all_entries = [
            dict(m.to_dict(), agent_id=m.agent_id) for m in machines.values()
        ] + [dict(p.to_dict(), agent_id=aid) for aid, p in pending.items()]
        latest = repository.latest_agent_reports()
        for entry in all_entries:
            entry["last_error"] = latest.get(entry["agent_id"])
        queued = len(queued_jobs())
        return {
            "total": len(machines),
            "alive": sum(1 for m in machines.values() if m.alive),
            "dead": sum(1 for m in machines.values() if not m.alive),
            "pending": len(pending),
            "queued": queued,
            "active_chat_streams": len(active_chat_streams),
            "rpc_clusters": [c.to_dict() for c in rpc_clusters.values()],
            "machines": all_entries,
        }

    @app.get("/stats")
    async def get_stats(_key: str = Depends(require_admin)):
        total_ok = sum(m.requests_ok for m in machines.values())
        total_err = sum(m.requests_err for m in machines.values())
        all_times = [t for m in machines.values() for t in m.response_times]
        return {
            "machines_total": len(machines),
            "machines_alive": sum(1 for m in machines.values() if m.alive),
            "requests_ok": total_ok,
            "requests_err": total_err,
            "avg_response_ms": round(statistics.mean(all_times), 1)
            if all_times
            else None,
        }

    @app.get("/stats/usage")
    async def get_usage_stats(
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
        bucket: str = "day",
        _key: str = Depends(require_admin),
    ):
        return repository.usage_stats(from_ts, to_ts, bucket)
