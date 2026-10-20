"""Authenticated endpoint exchange for LAN QUIC, without relaying model bytes."""
import ipaddress
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field


def private_ipv4(value):
    try:
        ip = ipaddress.ip_address(value)
        return ip.version == 4 and ip.is_private and not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified)
    except (ValueError, TypeError):
        return False


class QuicStats(BaseModel):
    sent_bytes: int = Field(default=0, ge=0, le=2**63-1)
    received_bytes: int = Field(default=0, ge=0, le=2**63-1)
    streams: int = Field(default=0, ge=0, le=1000000)
    peak_inflight_chunks: int = Field(default=0, ge=0, le=4)
    connected: bool = False


class QuicEndpoint(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    cluster_id: str = Field(min_length=1, max_length=128)
    port: int = Field(ge=1, le=65535, strict=True)
    certificate_pem: str = Field(default="", max_length=4096)
    stats: QuicStats = Field(default_factory=QuicStats)


def install_quic_routes(app, clusters, assignments, machines, require_agent):
    @app.post("/agent/rpc/quic/endpoint")
    async def endpoint(payload: QuicEndpoint, request: Request, _key=Depends(require_agent)):
        cluster = clusters.get(payload.cluster_id)
        assignment = assignments.get(payload.agent_id)
        if not cluster or cluster.status not in ("forming", "active") or cluster.transport != "quic":
            raise HTTPException(410, "QUIC cluster is no longer available")
        if (request.headers.get("X-Swarm-Agent-ID") != payload.agent_id or not assignment
                or assignment.get("cluster_id") != payload.cluster_id):
            raise HTTPException(403, "QUIC endpoint does not match an assigned agent")
        own = machines.get(payload.agent_id)
        if not own or not own.alive or not private_ipv4(own.lan_ip):
            raise HTTPException(409, "Assigned QUIC agent has no live private LAN address")
        if assignment["role"] == "worker":
            if not payload.certificate_pem.startswith("-----BEGIN CERTIFICATE-----\n"):
                raise HTTPException(422, "QUIC worker must publish its TLS certificate")
            peers = [cluster.primary_id]
        else:
            peers = cluster.worker_ids
            if payload.certificate_pem:
                raise HTTPException(422, "QUIC primary does not publish a server certificate")
        registered = {"agent_id": payload.agent_id, "ip": own.lan_ip,
                      "port": payload.port, "certificate_pem": payload.certificate_pem}
        previous = assignment.get("quic_endpoint")
        if previous and previous != registered:
            raise HTTPException(409, "QUIC endpoint cannot change during an active cluster")
        assignment["quic_endpoint"] = registered
        if not hasattr(cluster, "quic_stats"):
            cluster.quic_stats = {}
        cluster.quic_stats[payload.agent_id] = payload.stats.model_dump()
        return {"peers": [assignments[peer]["quic_endpoint"] for peer in peers
                           if peer in assignments and assignments[peer].get("cluster_id") == payload.cluster_id
                           and assignments[peer].get("quic_endpoint")]}
