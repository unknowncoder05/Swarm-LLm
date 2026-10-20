"""Shared bounded byte transport for local and regional RPC relays."""
import asyncio
import time
import uuid
from typing import Any

from fastapi import Depends, HTTPException, Request, Response

# Bounded, authenticated byte relay for machines that only allow outbound HTTPS.
# Each HTTP frame remains below typical reverse-proxy request limits.
RPC_RELAY_FRAME = 768 * 1024
RPC_RELAY_CONNECTIONS = 8

class RpcRelayConnection:
    def __init__(self):
        self.queues = {side: asyncio.Queue(maxsize=2) for side in ("primary", "worker")}
        self.send_locks = {side: asyncio.Lock() for side in self.queues}
        self.recv_locks = {side: asyncio.Lock() for side in self.queues}
        self.next_sequence = {side: 0 for side in self.queues}
        self.last_digest = {side: None for side in self.queues}
        self.inflight = {side: None for side in self.queues}
        self.closed = asyncio.Event()
        self.worker_connected = False

async def _relay_wait(connection: RpcRelayConnection, operation, timeout: float):
    task = asyncio.create_task(operation)
    closed = asyncio.create_task(connection.closed.wait())
    try:
        done, _ = await asyncio.wait((task, closed), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if closed in done:
            raise HTTPException(410, "RPC relay connection is closed")
        if task not in done:
            raise asyncio.TimeoutError()
        return task.result()
    finally:
        for pending in (task, closed):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, closed, return_exceptions=True)


def install_relay_routes(app, rpc_clusters, rpc_relays, require_agent):
    def _relay_access(request: Request, cluster_id: str, worker_id: str, side: str) -> Any:
        cluster = rpc_clusters.get(cluster_id)
        if not cluster or cluster.status == "dissolving" or cluster.transport != "relay" or \
                getattr(cluster, "expires_at", float("inf")) <= time.time():
            raise HTTPException(410, "RPC relay cluster is no longer available")
        if worker_id not in cluster.worker_ids or side not in ("primary", "worker"):
            raise HTTPException(403, "Invalid RPC relay member")
        expected = cluster.primary_id if side == "primary" else worker_id
        if request.headers.get("X-Swarm-Agent-ID") != expected:
            raise HTTPException(403, "RPC relay agent does not match assigned role")
        return cluster

    def _relay_connection(cluster_id: str, worker_id: str, connection_id: str) -> RpcRelayConnection:
        connection = rpc_relays.get((cluster_id, worker_id, connection_id))
        if not connection or connection.closed.is_set():
            raise HTTPException(410, "RPC relay connection is closed")
        return connection

    @app.post("/agent/rpc/relay/{cluster_id}/{worker_id}/{connection_id}")
    async def rpc_relay_open(request: Request, cluster_id: str, worker_id: str, connection_id: str,
                             _key: str = Depends(require_agent)):
        _relay_access(request, cluster_id, worker_id, "primary")
        try:
            uuid.UUID(connection_id)
        except ValueError:
            raise HTTPException(422, "Relay connection ID must be a UUID")
        identity = (cluster_id, worker_id, connection_id)
        if identity not in rpc_relays:
            count = sum(1 for key in rpc_relays if key[:2] == identity[:2])
            if count >= RPC_RELAY_CONNECTIONS:
                raise HTTPException(429, "Too many RPC relay connections")
            rpc_relays[identity] = RpcRelayConnection()
        return {"status": "ok"}

    @app.post("/agent/rpc/relay/{cluster_id}/{worker_id}/{connection_id}/{side}/send")
    async def rpc_relay_send(request: Request, cluster_id: str, worker_id: str, connection_id: str,
                             side: str, seq: int, _key: str = Depends(require_agent)):
        import hashlib
        cluster = _relay_access(request, cluster_id, worker_id, side)
        connection = _relay_connection(cluster_id, worker_id, connection_id)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > RPC_RELAY_FRAME:
                raise HTTPException(413, "RPC relay frame exceeds 768 KiB")
        if not data or seq < 0:
            raise HTTPException(422, "Relay requires a nonempty frame and nonnegative sequence")
        data = bytes(data)
        digest = hashlib.sha256(data).digest()
        async with connection.send_locks[side]:
            expected = connection.next_sequence[side]
            if seq == expected - 1:
                if digest != connection.last_digest[side]:
                    raise HTTPException(409, "Retried relay frame differs")
                return {"status": "ok"}
            if seq != expected:
                raise HTTPException(409, "Unexpected relay frame sequence")
            peer = "worker" if side == "primary" else "primary"
            try:
                await _relay_wait(connection, connection.queues[peer].put((seq, data)), 30)
            except asyncio.TimeoutError:
                raise HTTPException(503, "RPC relay receiver is not draining frames")
            connection.next_sequence[side] += 1
            connection.last_digest[side] = digest
            cluster.relay_bytes += len(data)
        return {"status": "ok"}

    @app.get("/agent/rpc/relay/{cluster_id}/{worker_id}/{connection_id}/{side}/recv")
    async def rpc_relay_receive(request: Request, cluster_id: str, worker_id: str, connection_id: str,
                                side: str, ack: int = -1, _key: str = Depends(require_agent)):
        _relay_access(request, cluster_id, worker_id, side)
        connection = _relay_connection(cluster_id, worker_id, connection_id)
        if side == "worker":
            connection.worker_connected = True
        async with connection.recv_locks[side]:
            frame = connection.inflight[side]
            if frame is not None and ack > frame[0]:
                raise HTTPException(409, "Relay acknowledgement is ahead of delivery")
            if frame is not None and ack == frame[0]:
                connection.inflight[side] = frame = None
            if frame is None:
                try:
                    frame = await _relay_wait(connection, connection.queues[side].get(), 25)
                except asyncio.TimeoutError:
                    return Response(status_code=204)
                connection.inflight[side] = frame
            return Response(frame[1], media_type="application/octet-stream",
                            headers={"X-Relay-Sequence": str(frame[0])})

    @app.post("/agent/rpc/relay/{cluster_id}/{worker_id}/{connection_id}/{side}/close")
    async def rpc_relay_close(request: Request, cluster_id: str, worker_id: str, connection_id: str,
                              side: str, _key: str = Depends(require_agent)):
        _relay_access(request, cluster_id, worker_id, side)
        connection = rpc_relays.pop((cluster_id, worker_id, connection_id), None)
        if connection:
            connection.closed.set()
        return {"status": "ok"}
