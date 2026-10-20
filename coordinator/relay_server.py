"""Regional RPC data plane; scheduling and inference stay on existing machines."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

try:
    from relay_transport import install_relay_routes
except ModuleNotFoundError:
    from coordinator.relay_transport import install_relay_routes

keys_file = os.environ.get("RELAY_KEYS_FILE")
keys = json.loads(Path(keys_file).read_text()) if keys_file else os.environ
AGENT_KEY = keys.get("AGENT_KEY", "")
CONTROL_KEY = keys.get("CONTROL_KEY", "")
if not AGENT_KEY or not CONTROL_KEY or AGENT_KEY == CONTROL_KEY:
    raise RuntimeError("Distinct AGENT_KEY and CONTROL_KEY are required")

clusters = {}
connections = {}
LEASE_SECONDS = 180
MAX_CLUSTERS = 4


def _authenticate(request: Request, expected: str):
    supplied = request.headers.get("X-API-Key", "")
    if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, "Invalid or missing API key")


def require_agent(request: Request):
    _authenticate(request, AGENT_KEY)


def require_control(request: Request):
    _authenticate(request, CONTROL_KEY)


def close_cluster(cluster_id):
    clusters.pop(cluster_id, None)
    for identity, connection in list(connections.items()):
        if identity[0] == cluster_id:
            connection.closed.set()
            connections.pop(identity, None)


async def expire_clusters():
    while True:
        await asyncio.sleep(15)
        for cluster_id, cluster in list(clusters.items()):
            if cluster.expires_at <= time.time():
                close_cluster(cluster_id)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(expire_clusters())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for cluster_id in list(clusters):
            close_cluster(cluster_id)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
install_relay_routes(app, clusters, connections, require_agent)


class ClusterRegistration(BaseModel):
    primary_id: str = Field(min_length=1, max_length=128)
    worker_ids: list[str] = Field(min_length=1, max_length=8)


@app.get("/healthz")
def health():
    return {"status": "ok", "service": "swarm-rpc-relay"}


@app.put("/control/clusters/{cluster_id}")
async def register_cluster(cluster_id: str, payload: ClusterRegistration,
                           _key=Depends(require_control)):
    if len(set(payload.worker_ids)) != len(payload.worker_ids) or payload.primary_id in payload.worker_ids:
        raise HTTPException(422, "Distinct primary and worker IDs are required")
    cluster = clusters.get(cluster_id)
    if cluster:
        if cluster.primary_id != payload.primary_id or cluster.worker_ids != payload.worker_ids:
            raise HTTPException(409, "Existing cluster membership cannot change")
    else:
        if len(clusters) >= MAX_CLUSTERS:
            raise HTTPException(429, "Relay cluster capacity reached")
        cluster = SimpleNamespace(primary_id=payload.primary_id, worker_ids=payload.worker_ids,
            status="active", transport="relay", relay_bytes=0, expires_at=0)
        clusters[cluster_id] = cluster
    cluster.expires_at = time.time() + LEASE_SECONDS
    return {"status": "ok", "lease_seconds": LEASE_SECONDS}


@app.get("/control/clusters/{cluster_id}")
async def inspect_cluster(cluster_id: str, _key=Depends(require_control)):
    cluster = clusters.get(cluster_id)
    if not cluster or cluster.expires_at <= time.time():
        close_cluster(cluster_id)
        raise HTTPException(410, "Relay cluster lease expired")
    # The coordinator renews this lease while polling worker assignments. An
    # unreachable/restarted coordinator cannot leave an unbounded orphan relay.
    cluster.expires_at = time.time() + LEASE_SECONDS
    return {"relay_bytes": cluster.relay_bytes, "connections": {
        worker: [identity[2] for identity, connection in connections.items()
                 if identity[:2] == (cluster_id, worker) and not connection.closed.is_set()]
        for worker in cluster.worker_ids}}


@app.delete("/control/clusters/{cluster_id}")
async def remove_cluster(cluster_id: str, _key=Depends(require_control)):
    close_cluster(cluster_id)
    return {"status": "ok"}
