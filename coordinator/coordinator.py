"""
peerfoundry coordinator
─────────────────────
Endpoints and required key:
  GET  /healthz                    — no auth (uptime probes)
  POST /register                   — AGENT_KEY or ADMIN_KEY
  POST /heartbeat                  — AGENT_KEY or ADMIN_KEY
  GET  /agent/jobs/next            — AGENT_KEY: poll for a pending job
  POST /agent/jobs/{id}/result     — AGENT_KEY: submit completed result
  GET  /machines                   — ADMIN_KEY
  GET  /stats                      — ADMIN_KEY
  GET  /v1/models                  — CONSUMER_KEY or ADMIN_KEY
  POST /v1/chat/completions        — CONSUMER_KEY or ADMIN_KEY

Architecture: pull-based. Agents poll for work; coordinator never
connects to agents. Works through NAT/restricted firewalls.

Security measures:
  - Three-key auth with constant-time comparison (no timing attacks)
  - Source IP derived from X-Real-IP set by nginx, never from payload
  - Rate limiting: consumer 60 req/min, registration 30 per 5 min (per IP)
  - Internal machine addresses never exposed in error responses
  - CORS configured for browser clients
"""

import asyncio
import base64
import json
import os
import secrets
import time
import uuid
import re
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path
from collections import defaultdict, OrderedDict
from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

try:
    from chat_admission import InvalidChatRequest, prepare_chat_request
except ModuleNotFoundError:
    from coordinator.chat_admission import InvalidChatRequest, prepare_chat_request
try:
    from relay_client import RelayProvisionerClient
except ModuleNotFoundError:
    from coordinator.relay_client import RelayProvisionerClient

try:
    from relay_policy import RelayPolicy
except ModuleNotFoundError:
    from coordinator.relay_policy import RelayPolicy
try:
    from rpc_performance import DEFAULT_RUNTIME, validate_runtime, model_runtime_profiles
except ModuleNotFoundError:
    from coordinator.rpc_performance import DEFAULT_RUNTIME, validate_runtime, model_runtime_profiles
try:
    from chat_stream import StreamBuffer, install_stream_routes, sse
except ModuleNotFoundError:
    from coordinator.chat_stream import StreamBuffer, install_stream_routes, sse

try:
    from api_models import (
        AgentStatusPayload, ContainerJobCreate, ContainerJobResult, HeartbeatPayload,
        ImageGenerationRequest, JobResultPayload, RegisterPayload, RpcLogPayload,
        RpcPreflightPayload, RpcReadyPayload, VideoGenerationRequest, _validate_container_job,
    )
    from models import MachineState, PendingAgent, Job, ImageJob, VideoJob, _chat_payload_bytes
    from persistence import CoordinatorRepository, _container_job_dict
    from security import ApiKeyAuth, _ct_match, _extract_key, _RateLimiter, _real_ip
    from catalog import _RPC_MODELS, _RPC_DEFAULT_CONTEXT, _ON_DEMAND_MODELS, IMAGE_MODELS, VIDEO_MODELS, MODEL_ALIASES, RPC_FALLBACK_MODELS
    from validation import validate_contract
    from placement import PlacementPolicy, _physical_host
    from admin_api import install_admin_routes
except ModuleNotFoundError:
    from coordinator.api_models import (
        AgentStatusPayload, ContainerJobCreate, ContainerJobResult, HeartbeatPayload,
        ImageGenerationRequest, JobResultPayload, RegisterPayload, RpcLogPayload,
        RpcPreflightPayload, RpcReadyPayload, VideoGenerationRequest, _validate_container_job,
    )
    from coordinator.models import MachineState, PendingAgent, Job, ImageJob, VideoJob, _chat_payload_bytes
    from coordinator.persistence import CoordinatorRepository, _container_job_dict
    from coordinator.security import ApiKeyAuth, _ct_match, _extract_key, _RateLimiter, _real_ip
    from coordinator.catalog import _RPC_MODELS, _RPC_DEFAULT_CONTEXT, _ON_DEMAND_MODELS, IMAGE_MODELS, VIDEO_MODELS, MODEL_ALIASES, RPC_FALLBACK_MODELS
    from coordinator.validation import validate_contract
    from coordinator.placement import PlacementPolicy, _physical_host
    from coordinator.admin_api import install_admin_routes

# ── config ────────────────────────────────────────────────────────────────────

# The lab may customize these estimates; keep each application instance isolated.
_RPC_MODELS = dict(_RPC_MODELS)
_ON_DEMAND_MODELS = dict(_ON_DEMAND_MODELS)
_RPC_DEFAULT_CONTEXT = dict(_RPC_DEFAULT_CONTEXT)
RPC_FALLBACK_MODELS = dict(RPC_FALLBACK_MODELS)

ADMIN_KEY    = os.environ.get("ADMIN_KEY", "")
AGENT_KEY    = os.environ.get("AGENT_KEY", "")
CONSUMER_KEY = os.environ.get("CONSUMER_KEY", "")

for _name, _val in [("ADMIN_KEY", ADMIN_KEY), ("AGENT_KEY", AGENT_KEY), ("CONSUMER_KEY", CONSUMER_KEY)]:
    if not _val:
        raise RuntimeError(f"{_name} environment variable must be set")

DEAD_AFTER_SEC        = 120
HEALTH_CHECK_INTERVAL = 10
REQUEST_TIMEOUT       = 120
DB_PATH               = os.environ.get("DB_PATH", "/data/coordinator.db")
CONTAINER_ARTIFACT_DIR = os.environ.get("CONTAINER_ARTIFACT_DIR", "/data/container-artifacts")
CONTAINER_LEASE_SEC    = int(os.environ.get("CONTAINER_LEASE_SEC", "300"))
MAX_ARTIFACT_BYTES     = int(os.environ.get("MAX_ARTIFACT_BYTES", str(500 * 1024 * 1024)))
RPC_RELAY_URL = os.environ.get("RPC_RELAY_URL", "").rstrip("/")
RPC_RELAY_ADMIN_KEY = os.environ.get("RPC_RELAY_ADMIN_KEY", "")
RPC_RELAY_EXPIRES_AT = float(os.environ.get("RPC_RELAY_EXPIRES_AT", "0"))
RPC_RELAY_PROVISIONER_URL = os.environ.get("RPC_RELAY_PROVISIONER_URL", "").rstrip("/")
RPC_CLOUD_RELAY_ENABLED = os.environ.get("RPC_CLOUD_RELAY_ENABLED", "true").lower() == "true"
relay_policy = RelayPolicy(os.environ.get("RPC_RELAYS", "[]"), RPC_CLOUD_RELAY_ENABLED)
relay_provisioner = (RelayProvisionerClient(RPC_RELAY_PROVISIONER_URL, os.environ.get("RPC_RELAY_PROVISIONER_KEY", ""))
                     if RPC_RELAY_PROVISIONER_URL and RPC_CLOUD_RELAY_ENABLED else None)
_rpc_formation_lock = asyncio.Lock()
try:
    RPC_IDLE_TIMEOUT = int(os.environ.get("RPC_IDLE_TIMEOUT_SEC", "300"))
except ValueError:
    raise RuntimeError("RPC_IDLE_TIMEOUT_SEC must be an integer between 60 and 3600") from None
if not 60 <= RPC_IDLE_TIMEOUT <= 3600:
    raise RuntimeError("RPC_IDLE_TIMEOUT_SEC must be an integer between 60 and 3600")
RPC_DEFAULT_TRANSPORT = os.environ.get("RPC_DEFAULT_TRANSPORT") or ("relay" if RPC_RELAY_URL or relay_provisioner else "direct")
RPC_RUNTIME_PROFILES = model_runtime_profiles(os.environ.get("RPC_MODEL_RUNTIME_PROFILES", ""))
if RPC_DEFAULT_TRANSPORT not in ("auto", "direct", "relay", "quic"):
    raise RuntimeError("RPC_DEFAULT_TRANSPORT must be auto, direct, relay or quic")
if RPC_RELAY_URL:
    _relay_url = urlsplit(RPC_RELAY_URL)
    if _relay_url.scheme != "https" or not _relay_url.hostname or _relay_url.username or \
            _relay_url.password or _relay_url.query or _relay_url.fragment or not RPC_RELAY_ADMIN_KEY:
        raise RuntimeError("RPC_RELAY_URL requires an HTTPS URL and RPC_RELAY_ADMIN_KEY")

# ── auth ──────────────────────────────────────────────────────────────────────

auth = ApiKeyAuth(ADMIN_KEY, AGENT_KEY, CONSUMER_KEY)
require_agent = auth.require_agent
require_consumer = auth.require_consumer
require_admin = auth.require_admin


# ── rate limiter ──────────────────────────────────────────────────────────────


_consumer_rl = _RateLimiter(max_calls=60,  window_sec=60)   # 60/min per IP
_register_rl = _RateLimiter(max_calls=30,  window_sec=300)  # 30 per 5 min per IP (supports 10+ machines behind same NAT)


# ── sqlite persistence ────────────────────────────────────────────────────────

repository = CoordinatorRepository(lambda: DB_PATH)
_db = repository.db
_init_db = repository.init_db
_upsert_machine = repository.upsert_machine
_log_usage = repository.log_usage
_save_image_job = repository.save_image_job
_save_video_job = repository.save_video_job
_load_media_jobs = repository.load_media_jobs
_load_machines = repository.load_machines


def _cancel_job(job_id: str, reason: str) -> None:
    """Unblock the consumer; keep an executing worker reserved until it acknowledges.

    In particular native RPC inference does not stop when its consumer disconnects.
    """
    job = jobs.pop(job_id, None)
    if not job or job.done_event.is_set():
        return
    job.cancelled = True
    job.error     = f"cancelled:{reason}"
    job.done_event.set()
    _prune_chat_queue(job.model)
    print(f"[coordinator] Job {job_id[:8]} cancelled ({reason})")


# ── state ─────────────────────────────────────────────────────────────────────

machines: dict[str, MachineState] = {}

# Agents that are still setting up (not yet registered)

pending: dict[str, PendingAgent] = {}

placement = PlacementPolicy(machines, _RPC_MODELS, _ON_DEMAND_MODELS, lambda: _queued_chat_jobs(), DEAD_AFTER_SEC)
_on_demand_capacity = placement.on_demand_capacity
_worker_available = placement.worker_available
_single_context_limit = placement.single_context_limit
_can_serve_single = placement.can_serve_single
_select_rpc_machines = placement.select_rpc_machines
_automatic_pair = placement.automatic_pair

# ── RPC cluster state ─────────────────────────────────────────────────────────


def _queued_chat_jobs(model: Optional[str] = None):
    return [job for job in jobs.values() if not job.done_event.is_set() and not job.cancelled
            and job.assigned_agent_id is None and (model is None or job.model == model)]


def _prune_chat_queue(model: str) -> None:
    """Remove dead entries as they are cancelled, even while workers stay busy."""
    queue = job_queues.get(model)
    if queue is None:
        return
    live = []
    for _ in range(queue.qsize()):
        job = queue.get_nowait()
        queue.task_done()
        if not job.cancelled and not job.done_event.is_set() and job.assigned_agent_id is None:
            live.append(job)
    for job in live:
        queue.put_nowait(job)


def _check_chat_queue_capacity(model: str, payload_bytes: int = 0) -> None:
    waiting = _queued_chat_jobs()
    model_jobs = [job for job in waiting if job.model == model]
    if (len(waiting) >= CHAT_QUEUE_LIMIT_TOTAL or len(model_jobs) >= CHAT_QUEUE_LIMIT_PER_MODEL
            or sum(job.input_bytes for job in waiting) + payload_bytes > CHAT_QUEUE_BYTES_TOTAL
            or sum(job.input_bytes for job in model_jobs) + payload_bytes > CHAT_QUEUE_BYTES_PER_MODEL):
        raise HTTPException(429, "Inference queue is full; retry when workers finish pending work",
                            headers={"Retry-After": "5"})


def _release_chat_slot(job_id: str, claim_token: Optional[str]) -> None:
    for machine in machines.values():
        if machine.busy_job_id == job_id and (claim_token == machine.busy_claim_token
                or (claim_token is None and machine.busy_claim_attempt == 1)):
            machine.completed_job_ids.append(job_id)
            machine.busy = False
            machine.busy_job_id = None
            machine.busy_claim_token = None
            machine.busy_claim_request_id = None
            machine.busy_chat_model = None
            if machine.phase == "DOWNLOADING":
                machine.phase = "RUNNING"
            if machine.rpc_cluster_id in rpc_clusters:
                rpc_clusters[machine.rpc_cluster_id].last_job_at = time.time()


def _requeue_chat_from(machine: MachineState) -> None:
    job = jobs.get(machine.busy_job_id)
    if job and not job.done_event.is_set() and job.assigned_agent_id == machine.agent_id:
        job.assigned_agent_id = None
        job.claim_token = None
        job_queues.setdefault(job.model, asyncio.Queue()).put_nowait(job)

class RpcCluster:
    def __init__(self, cluster_id: str, model: str, primary_id: str, worker_ids: list[str], transport: str = "direct",
                 ctx_size: int = 4096):
        self.cluster_id  = cluster_id
        self.model       = model
        self.primary_id  = primary_id
        self.worker_ids  = worker_ids
        self.status      = "forming"   # forming | active | dissolving
        self.formed_at   = time.time()
        self.last_job_at: Optional[float] = None
        self.primary_ready = False
        self.transport = transport
        self.ctx_size = ctx_size
        self.relay_bytes = 0
        self.relay_url: Optional[str] = None
        self.relay_control_key: Optional[str] = None
        self.relay_expires_at: Optional[float] = None
        self.provisioner_last_ok = time.time()
        self.relay_ready = True
        self.relay_last_ok = time.time()
        self.relay_removed = False
        self.dissolved_at: Optional[float] = None
        self.dissolve_reason: Optional[str] = None
        self.automatic = False
        self.route_id = transport
        self.preflight = {}
        self.runtime_profile = dict(DEFAULT_RUNTIME)

    def to_dict(self) -> dict:
        return {
            "cluster_id":  self.cluster_id,
            "model":       self.model,
            "primary_id":  self.primary_id,
            "worker_ids":  self.worker_ids,
            "status":      self.status,
            "formed_at":   self.formed_at,
            "last_job_at": self.last_job_at,
            "primary_ready": self.primary_ready,
            "transport": self.transport,
            "route_id": self.route_id,
            "automatic": self.automatic,
            "preflight": dict(self.preflight),
            "runtime_profile": dict(self.runtime_profile),
            "ctx_size": self.ctx_size,
            "idle_timeout_sec": RPC_IDLE_TIMEOUT,
            "relay_expires_at": self.relay_deadline if self.relay_url else None,
            "relay_bytes": self.relay_bytes,
            "relay_url": self.relay_url,
            "quic_stats": getattr(self, "quic_stats", {}),
            "dissolved_at": self.dissolved_at,
            "dissolve_reason": self.dissolve_reason,
        }

    @property
    def relay_deadline(self):
        return self.relay_expires_at if self.relay_expires_at is not None else RPC_RELAY_EXPIRES_AT

# cluster_id → RpcCluster
rpc_clusters: dict[str, RpcCluster] = {}
# agent_id → assignment dict delivered via /agent/rpc/assignment
agent_rpc_assignments: dict[str, dict] = {}

# Shared by the local coordinator and the optional relay service.
try:
    from relay_transport import RpcRelayConnection, RPC_RELAY_FRAME, RPC_RELAY_CONNECTIONS, install_relay_routes
except ModuleNotFoundError:
    from coordinator.relay_transport import RpcRelayConnection, RPC_RELAY_FRAME, RPC_RELAY_CONNECTIONS, install_relay_routes

try:
    from quic_control import install_quic_routes, private_ipv4
except ModuleNotFoundError:
    from coordinator.quic_control import install_quic_routes, private_ipv4
try:
    from rpc_handoff import restore_quic_handoff
except ModuleNotFoundError:
    from coordinator.rpc_handoff import restore_quic_handoff

rpc_relays: dict[tuple[str, str, str], RpcRelayConnection] = {}


def _regional_relay_call(cluster: RpcCluster, method: str, payload=None):
    request = urllib.request.Request(
        f"{cluster.relay_url}/control/clusters/{cluster.cluster_id}", method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"X-API-Key": cluster.relay_control_key or RPC_RELAY_ADMIN_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        result = json.load(response)
    cluster.relay_last_ok = time.time()
    if "relay_bytes" in result:
        cluster.relay_bytes = result["relay_bytes"]
    return result

RPC_FORM_TIMEOUT  = 1800.0   # max seconds to wait for cluster to become active
# Idle lifetime is configurable above; it never overrides a regional deadline.

def _find_rpc_cluster_for_model(model: str, agent_ids=None) -> Optional[RpcCluster]:
    candidates = [c for c in rpc_clusters.values() if c.model == model and c.status in ("forming", "active")
                  and not getattr(c, "route_pending", False)
                  and (agent_ids is None or set(agent_ids) == {c.primary_id, *c.worker_ids})]
    def rank(cluster):
        pending = sum(job.rpc_cluster_id == cluster.cluster_id and not job.done_event.is_set()
                      and not job.cancelled for job in jobs.values())
        primary = machines.get(cluster.primary_id)
        return (cluster.status != "active", pending, bool(primary and primary.busy), cluster.formed_at)
    return min(candidates, key=rank) if candidates else None


def _rpc_cluster_for_machine(machine, model):
    cluster = rpc_clusters.get(machine.rpc_cluster_id)
    return cluster if cluster and cluster.model == model else None


def _rpc_memory_required(model, context=None, explicit_transport=False):
    if model in _RPC_MODELS:
        return _RPC_MODELS[model]
    if model in RPC_FALLBACK_MODELS:
        if (explicit_transport or _find_rpc_cluster_for_model(model)
                or not any(m.alive and not m.in_rpc and _can_serve_single(m, model, context)
                           for m in machines.values())):
            return RPC_FALLBACK_MODELS[model]
    return None


def _job_matches_rpc_cluster(job, cluster):
    return job.rpc_cluster_id == cluster.cluster_id or (
        job.rpc_cluster_id is None and job.model in _RPC_MODELS and job.model == cluster.model)


def _form_rpc_cluster(model: str, required_vram: float, ctx_size: int = 4096,
                      transport: str = "direct", regional=None, agent_ids=None, runtime=None) -> Optional[RpcCluster]:
    use_regional = transport == "relay" and (regional or (RPC_CLOUD_RELAY_ENABLED and RPC_RELAY_URL and
        (not RPC_RELAY_EXPIRES_AT or time.time() < RPC_RELAY_EXPIRES_AT - 300)))
    selected = _select_rpc_machines(required_vram, bool(use_regional), agent_ids=agent_ids)
    if not selected:
        return None
    if transport == "quic" and (len(selected) != 2 or any(
            not (m.runtime or {}).get("rpc_quic") or not private_ipv4(m.lan_ip) for m in selected)):
        return None
    primary  = selected[0]
    workers  = selected[1:]
    profile = validate_runtime(RPC_RUNTIME_PROFILES.get(model, {}) if runtime is None else runtime)
    if profile != DEFAULT_RUNTIME and not (primary.runtime or {}).get("rpc_tuning"):
        return None
    cid      = str(uuid.uuid4())
    cluster  = RpcCluster(cid, model, primary.agent_id, [w.agent_id for w in workers], transport=transport,
                          ctx_size=ctx_size)
    cluster.runtime_profile = profile
    if use_regional and workers:
        cluster.relay_url = regional["url"] if regional else RPC_RELAY_URL
        cluster.relay_control_key = regional["control_key"] if regional else None
        cluster.relay_expires_at = regional["expires_at"] if regional else None
        cluster.relay_ready = False

    for m in selected:
        m.in_rpc         = True
        m.rpc_cluster_id = cid

    for i, w in enumerate(workers):
        agent_rpc_assignments[w.agent_id] = {
            "role":       "worker",
            "cluster_id": cid,
            "rpc_port":   50052 + i,
            "listen_host": "127.0.0.1" if transport in ("relay", "quic") else w.lan_ip,
            "transport": transport,
            "primary_ip": primary.lan_ip,
            "model":      model,
            "dissolve":   False,
        }

    worker_endpoints = [
        f"127.0.0.1:{50552 + i}" if transport == "relay" else f"{w.lan_ip or w.ip}:{50052 + i}"
        for i, w in enumerate(workers)
    ]
    agent_rpc_assignments[primary.agent_id] = {
        "role":       "primary",
        "cluster_id": cid,
        "model":      model,
        "workers":    worker_endpoints,
        "llama_port": 18080,
        "ctx_size":   ctx_size,
        "transport": transport,
        "load_timeout": 2700 if transport == "relay" else 900,
        "relay_workers": [{"agent_id": w.agent_id, "local_port": 50552 + i} for i, w in enumerate(workers)]
                         if transport == "relay" else [],
        "dissolve":   False,
    }
    if (primary.runtime or {}).get("rpc_tuning"):
        agent_rpc_assignments[primary.agent_id]["runtime_profile"] = profile

    if cluster.relay_url:
        for member in selected:
            agent_rpc_assignments[member.agent_id]["relay_url"] = cluster.relay_url
    if transport == "quic":
        # The initial transport supports one primary/worker pair. Do not
        # silently under-allocate a larger cluster or publish pair secrets.
        token = secrets.token_hex(32)
        for member in selected:
            agent_rpc_assignments[member.agent_id].update(lan_ip=member.lan_ip, quic_token=token)
    if all((member.runtime or {}).get("rpc_preflight") for member in selected) and workers:
        for member in selected:
            agent_rpc_assignments[member.agent_id].update(
                preflight_required=True)
        token = secrets.token_hex(32)
        for member in selected:
            agent_rpc_assignments[member.agent_id]["preflight_token"] = token
    rpc_clusters[cid] = cluster
    print(f"[rpc] cluster {cid[:8]} forming: primary={primary.hostname} workers={[w.hostname for w in workers]} model={model}")
    return cluster


async def _form_managed_rpc_cluster(model, required_vram, ctx_size, request=None):
    # Boot before making assignments visible, and serialize concurrent first use.
    async with _rpc_formation_lock:
        if request and await request.is_disconnected():
            raise HTTPException(499, "Client disconnected during relay provisioning")
        existing = _find_rpc_cluster_for_model(model)
        if existing:
            if ctx_size > existing.ctx_size:
                raise HTTPException(409, "A smaller RPC context was formed while the relay was starting")
            if existing.relay_url and existing.relay_deadline and time.time() >= existing.relay_deadline - 300:
                raise HTTPException(503, "Relay is nearing its runtime limit", headers={"Retry-After": "30"})
            return existing, False
        selected = _select_rpc_machines(required_vram, True)
        if not selected:
            return None, False
        regional = None
        if len(selected) > 1:
            try:
                regional = await relay_provisioner.ensure()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise HTTPException(503, "Relay provisioning is unavailable; retry shortly",
                                    headers={"Retry-After": "30"}) from None
        if request and await request.is_disconnected():
            raise HTTPException(499, "Client disconnected during relay provisioning")
        cluster = _form_rpc_cluster(model, required_vram, ctx_size, "relay", regional=regional)
        if cluster and regional:
            cluster.route_id = "managed-relay"
        return cluster, cluster is not None


async def _form_automatic_rpc_cluster(model, required_vram, ctx_size, agent_ids=None):
    async with _rpc_formation_lock:
        existing = _find_rpc_cluster_for_model(model, agent_ids)
        if existing:
            return existing if existing.ctx_size >= ctx_size else None
        return await _create_automatic_rpc_cluster(model, required_vram, ctx_size, agent_ids)


async def _create_automatic_rpc_cluster(model, required_vram, ctx_size, agent_ids=None):
    selected = _automatic_pair(required_vram, agent_ids)
    if not selected:
        return None
    ids = [m.agent_id for m in selected]
    quic = all((m.runtime or {}).get("rpc_quic") and private_ipv4(m.lan_ip) for m in selected)
    routes = relay_policy.candidates(ids, quic=quic, managed=bool(relay_provisioner))
    if not routes:
        raise HTTPException(503, "GPU capacity is available, but compatible routes are cooling down after earlier failures. "
                            "Waiting for a working connection between the machines.",
                            headers={"X-Swarm-Status": "waiting_for_route", "Retry-After": "5"})
    for route in routes:
        regional = {"url": route.url, "control_key": route.control_key, "expires_at": 0} if route.url else None
        if route.identity == "managed-relay":
            try:
                deployment = await relay_provisioner.ensure()
                regional = {"url": deployment["url"], "control_key": deployment["control_key"],
                            "expires_at": deployment["expires_at"]}
            except Exception:
                relay_policy.reject(route.identity, ids)
                continue
        cluster = _form_rpc_cluster(model, required_vram, ctx_size, route.transport,
                                    regional=regional, agent_ids=ids)
        if not cluster:
            continue
        cluster.route_id = route.identity
        if regional:
            cluster.route_pending = True
            try:
                await asyncio.to_thread(_regional_relay_call, cluster, "PUT", {
                    "primary_id": cluster.primary_id, "worker_ids": cluster.worker_ids})
                if route.identity == "managed-relay":
                    await relay_provisioner.renew(cluster.cluster_id)
                cluster.relay_ready = True
                cluster.route_pending = False
            except BaseException as exc:
                relay_policy.reject(route.identity, ids)
                # External assignments are hidden until control registration succeeds.
                for member in selected:
                    if member.rpc_cluster_id == cluster.cluster_id:
                        member.in_rpc = False
                        member.rpc_cluster_id = None
                        agent_rpc_assignments.pop(member.agent_id, None)
                rpc_clusters.pop(cluster.cluster_id, None)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                continue
        cluster.automatic = True
        return cluster
    return None


def _enqueue_rpc_once(job):
    queue = job_queues.setdefault(job.model, asyncio.Queue())
    pending = []
    while not queue.empty():
        item = queue.get_nowait()
        if item.id != job.id:
            pending.append(item)
    for item in pending:
        queue.put_nowait(item)
    queue.put_nowait(job)


async def _recover_rpc_jobs_once():
    for job in list(jobs.values()):
        if not getattr(job, "rpc_recovering", False) or job.cancelled or job.done_event.is_set():
            continue
        old = rpc_clusters.get(job.rpc_cluster_id)
        if old and any(agent_rpc_assignments.get(aid, {}).get("cluster_id") == old.cluster_id
                       for aid in [old.primary_id, *old.worker_ids]):
            continue  # Native role owners must acknowledge teardown before another attempt.
        if time.time() - job.created_at > (job.rpc_timeout or JOB_TIMEOUT) or job.rpc_recoveries > 3:
            _cancel_job(job.id, "rpc_recovery_exhausted")
            continue
        ctx = job.body.get("options", {}).get("num_ctx", 4096)
        candidates = [c for c in rpc_clusters.values() if c.model == job.model and c.status in ("forming", "active")
                 and (c.status == "forming" or c.primary_ready) and c.ctx_size >= ctx
                 and (not job.rpc_agent_ids or set(job.rpc_agent_ids) == {c.primary_id, *c.worker_ids})
                 and all(aid in machines and machines[aid].alive for aid in [c.primary_id, *c.worker_ids])
                 and not (c.relay_url and c.relay_deadline and c.relay_deadline <= time.time() + 300)]
        cluster = min(candidates, key=lambda c: (c.status != "active",
            sum(j.rpc_cluster_id == c.cluster_id and not j.done_event.is_set() for j in jobs.values()),
            bool(machines[c.primary_id].busy))) if candidates else None
        if not cluster:
            required = _RPC_MODELS.get(job.model, RPC_FALLBACK_MODELS.get(job.model))
            cluster = await _form_automatic_rpc_cluster(job.model, required, ctx, job.rpc_agent_ids)
        if not cluster:
            continue
        job.rpc_cluster_id = cluster.cluster_id
        job.rpc_timeout = max(job.rpc_timeout or JOB_TIMEOUT,
                              JOB_TIMEOUT * (2 if cluster.transport == "relay" else 1))
        job.assigned_agent_id = None
        job.claim_token = None
        if job.live_stream is not None:
            job.live_stream = StreamBuffer()
        job.rpc_recovering = False
        _enqueue_rpc_once(job)


async def _rpc_recovery_loop():
    while True:
        await asyncio.sleep(3)
        try:
            await _recover_rpc_jobs_once()
        except Exception as exc:
            print(f"[rpc] recovery will retry ({type(exc).__name__})")


def _dissolve_cluster(cluster: RpcCluster, reason: str = "idle") -> None:
    if cluster.status == "dissolving":
        return
    cluster.status = "dissolving"
    cluster.dissolved_at = time.time()
    cluster.dissolve_reason = reason
    recover = cluster.automatic and reason in {"preflight_failed", "relay_offline", "worker_aborted",
        "primary_aborted", "member_offline", "form_timeout", "inference_failed", "provisioner_unreachable"}
    if recover and reason in {"preflight_failed", "relay_offline", "worker_aborted", "member_offline", "provisioner_unreachable"}:
        relay_policy.reject(cluster.route_id, [cluster.primary_id, *cluster.worker_ids])
    for job in list(jobs.values()):
        belongs = job.rpc_cluster_id == cluster.cluster_id
        if job.rpc_cluster_id is None and job.model in _RPC_MODELS and job.model == cluster.model:
            belongs = (job.assigned_agent_id == cluster.primary_id or not any(
                other is not cluster and other.model == cluster.model and other.status in ("forming", "active")
                for other in rpc_clusters.values()))
        if belongs and not job.done_event.is_set():
            if recover and not job.cancelled and not getattr(job, "stream_started", False) and getattr(job, "rpc_recoveries", 0) < 3:
                job.rpc_recovering = True
                job.rpc_recoveries = getattr(job, "rpc_recoveries", 0) + 1
                _remember_terminal_claim(job.id, job.claim_token)
                job.claim_token = None
            else:
                _cancel_job(job.id, f"rpc_{reason}")
    for identity, connection in list(rpc_relays.items()):
        if identity[0] == cluster.cluster_id:
            connection.closed.set()
            rpc_relays.pop(identity, None)
    for aid in [cluster.primary_id] + cluster.worker_ids:
        if agent_rpc_assignments.get(aid, {}).get("cluster_id") == cluster.cluster_id:
            agent_rpc_assignments[aid]["dissolve"] = True
        # Do NOT clear in_rpc here — the agent is still running llama.cpp.
        # in_rpc is cleared in /agent/rpc/done once the process is killed.
    print(f"[rpc] cluster {cluster.cluster_id[:8]} dissolving ({reason})")

async def _rpc_lifecycle_loop() -> None:
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for cid, cluster in list(rpc_clusters.items()):
            members = [cluster.primary_id] + cluster.worker_ids
            if relay_provisioner and cluster.route_id == "managed-relay" and cluster.status in ("forming", "active"):
                try:
                    await relay_provisioner.renew(cid)
                    cluster.provisioner_last_ok = time.time()
                except Exception:
                    print(f"[rpc] could not renew provisioner lease for {cid[:8]}")
                    if now - cluster.provisioner_last_ok > 120:
                        _dissolve_cluster(cluster, "provisioner_unreachable")
            if cluster.status in ("forming", "active") and any(
                    aid not in machines or now - machines[aid].last_heartbeat > DEAD_AFTER_SEC
                    for aid in members):
                _dissolve_cluster(cluster, "member_offline")
                continue
            if cluster.relay_url and cluster.relay_ready and cluster.status in ("forming", "active"):
                try:
                    await asyncio.to_thread(_regional_relay_call, cluster, "GET")
                except Exception:
                    if now - cluster.relay_last_ok > DEAD_AFTER_SEC:
                        _dissolve_cluster(cluster, "relay_offline")
                        continue
            if cluster.status == "forming":
                if now - cluster.formed_at > RPC_FORM_TIMEOUT * (2 if cluster.transport == "relay" else 1):
                    _dissolve_cluster(cluster, "form_timeout")
            elif cluster.status == "active":
                last = cluster.last_job_at or cluster.formed_at
                pending_work = any(_job_matches_rpc_cluster(job, cluster)
                                   and not job.done_event.is_set() for job in jobs.values())
                idle = not pending_work and not any(machines[aid].busy for aid in members if aid in machines)
                if idle and cluster.relay_url and cluster.relay_deadline and now >= cluster.relay_deadline - 300:
                    _dissolve_cluster(cluster, "relay_deadline")
                elif idle and now - last > RPC_IDLE_TIMEOUT:
                    _dissolve_cluster(cluster, "idle")
            elif cluster.status == "dissolving":
                if cluster.relay_url and not cluster.relay_removed:
                    try:
                        await asyncio.to_thread(_regional_relay_call, cluster, "DELETE")
                        cluster.relay_removed = True
                    except Exception:
                        pass  # the regional lease also removes orphaned data
                # Clean up once all members have called /agent/rpc/done
                all_done = all(
                    agent_rpc_assignments.get(aid, {}).get("cluster_id") != cid
                    for aid in [cluster.primary_id] + cluster.worker_ids
                )
                # Also force-clean if dissolve is stuck (agent went offline)
                dissolve_stuck = now - (cluster.dissolved_at or now) > 300
                if all_done or dissolve_stuck:
                    for aid in [cluster.primary_id] + cluster.worker_ids:
                        if agent_rpc_assignments.get(aid, {}).get("cluster_id") == cid:
                            agent_rpc_assignments.pop(aid, None)
                        m = machines.get(aid)
                        if m and m.rpc_cluster_id == cid:
                            m.in_rpc         = False
                            m.rpc_cluster_id = None
                    rpc_clusters.pop(cid, None)
                    reason = "all_done" if all_done else "stuck_timeout"
                    print(f"[rpc] cluster {cid[:8]} removed ({reason})")

# ── job queue (pull-based dispatch) ──────────────────────────────────────────

JOB_TIMEOUT = 1800  # seconds consumer waits for a result (30 min — covers long thinking/reasoning models)
CHAT_QUEUE_LIMIT_PER_MODEL = 8
CHAT_QUEUE_LIMIT_TOTAL = 64
CHAT_QUEUE_BYTES_PER_MODEL = 8 * 1024 * 1024
CHAT_QUEUE_BYTES_TOTAL = 32 * 1024 * 1024
CHAT_REQUEST_MAX_BYTES = 2 * 1024 * 1024


jobs:       dict[str, Job]            = {}
job_queues: dict[str, asyncio.Queue]  = {}  # model → queue of Job
terminal_chat_claims: OrderedDict = OrderedDict()
active_chat_streams: dict[str, Optional[str]] = {}


def _remember_terminal_claim(job_id: str, claim_token: Optional[str]) -> None:
    """Fence delayed heartbeats even when completion precedes recovery telemetry."""
    key = (job_id, claim_token)
    terminal_chat_claims[key] = None
    terminal_chat_claims.move_to_end(key)
    while len(terminal_chat_claims) > 4096:
        terminal_chat_claims.popitem(last=False)


async def _read_chat_request(request: Request) -> dict:
    """Bound streamed input before parsing; media keeps its separate upload limits."""
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length") from None
        if declared < 0:
            raise HTTPException(400, "Invalid Content-Length")
        if declared > CHAT_REQUEST_MAX_BYTES:
            raise HTTPException(413, "Chat request exceeds 2 MiB; reduce the prompt or tool definitions")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > CHAT_REQUEST_MAX_BYTES:
            raise HTTPException(413, "Chat request exceeds 2 MiB; reduce the prompt or tool definitions")
        data.extend(chunk)
    try:
        return prepare_chat_request(json.loads(data))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON body") from None
    except InvalidChatRequest as exc:
        raise HTTPException(422, str(exc)) from None


async def _await_chat_completion(job: Job, request: Request, user_id: str, started_at: float) -> bool:
    """Own consumer waiting/cancellation while leaving executing GPU leases intact."""
    try:
        while True:
            try:
                await asyncio.wait_for(job.done_event.wait(), timeout=3.0)
                return True
            except asyncio.TimeoutError:
                cluster = (rpc_clusters.get(job.rpc_cluster_id) if job.rpc_cluster_id else
                           _find_rpc_cluster_for_model(job.model) if job.model in _RPC_MODELS else None)
                capacity = bool(cluster) or getattr(job, "rpc_recovering", False) or any(
                    m.alive and not m.in_rpc and _can_serve_single(m, job.model) for m in machines.values())
                timeout = JOB_TIMEOUT * (2 if cluster and cluster.transport == "relay" else 1) if capacity else 60
                if capacity and job.rpc_timeout:
                    timeout = max(timeout, job.rpc_timeout)
                if time.time() - started_at >= timeout:
                    _cancel_job(job.id, "timeout")
                    _log_usage(job.model, user_id, 0, 0, None, False, finish_reason="timeout")
                    alive_models = sorted({m.model for m in machines.values() if m.alive})
                    raise HTTPException(503, f"No agent completed '{job.model}' in time. Alive agents: {alive_models}")
                if await request.is_disconnected():
                    _cancel_job(job.id, "client_disconnected")
                    return False
    except asyncio.CancelledError:
        _cancel_job(job.id, "request_cancelled")
        raise

# ── media (image + video) ──────────────────────────────────────────────────────


IMAGE_DIR = os.environ.get("IMAGE_DIR", "/data/images")
VIDEO_DIR = os.environ.get("VIDEO_DIR", "/data/videos")


image_jobs:       dict[str, ImageJob]      = {}
video_jobs:       dict[str, VideoJob]      = {}
image_job_queues: dict[str, asyncio.Queue] = {}  # model → queue of ImageJob
video_job_queues: dict[str, asyncio.Queue] = {}  # model → queue of VideoJob


def _fail_media_job(job, reason: str) -> None:
    # A heartbeat outage is not proof the GPU process stopped. Do not start a
    # duplicate generation elsewhere; progress polling will request an abort.
    job.status = "failed"
    job.error = reason
    if isinstance(job, ImageJob):
        job.done_event.set()
        _save_image_job(job)
    else:
        _save_video_job(job)


def _release_media_slot(job_id: str) -> None:
    for machine in machines.values():
        if machine.busy_job_id == job_id:
            machine.completed_job_ids.append(job_id)
            machine.busy = False
            machine.busy_job_id = None
            machine.media_phase = "IDLE"
            machine.media_model = None
            machine.media_updated_at = time.time()

# ── app ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="PeerFoundry coordinator", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type", "Authorization"],
)

# ── agent auto-update ─────────────────────────────────────────────────────────

_AGENT_PY_URL = os.environ.get("AGENT_SOURCE_URL", "")
_latest_agent_py: Optional[str] = None

async def _agent_update_loop():
    global _latest_agent_py
    if os.environ.get("AGENT_SOURCE_PATH") or not _AGENT_PY_URL:
        return  # source must be mounted or explicitly configured by the operator
    import urllib.request
    while True:
        try:
            with urllib.request.urlopen(_AGENT_PY_URL, timeout=15) as r:
                _latest_agent_py = r.read().decode("utf-8")
        except Exception as e:
            print(f"[agent-update] fetch failed: {e}")
        await asyncio.sleep(300)   # refresh every 5 min

@app.get("/agent/agent.py")
async def serve_agent_py(_key: str = Depends(require_agent)):
    from fastapi.responses import PlainTextResponse
    source_path = os.environ.get("AGENT_SOURCE_PATH")
    if source_path:
        try:
            return PlainTextResponse(Path(source_path).read_text(encoding="utf-8"), media_type="text/x-python")
        except OSError:
            raise HTTPException(503, "Configured agent source is unavailable")
    if _latest_agent_py is None:
        raise HTTPException(503, "agent.py not yet fetched")
    return PlainTextResponse(_latest_agent_py, media_type="text/x-python")


# ── background health loop ────────────────────────────────────────────────────

async def _health_loop():
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        for m in list(machines.values()):
            was_alive = m.alive
            if m.last_seen_ago > DEAD_AFTER_SEC:
                m.alive = False
                if was_alive:
                    print(f"[coordinator] {m.hostname} DEAD — no heartbeat for {m.last_seen_ago:.0f}s")
                    # Re-queue any in-flight job so another agent can pick it up.
                    jid = m.busy_job_id
                    if jid:
                        if jid in jobs:
                            job = jobs[jid]
                            if not job.done_event.is_set():
                                _requeue_chat_from(m)
                                print(f"[coordinator] Re-queued chat job {jid[:8]} ({job.model}) — {m.hostname} went DEAD")
                        elif jid in image_jobs:
                            job = image_jobs[jid]
                            if job.status == "processing":
                                _fail_media_job(job, "Worker heartbeat lost; generation was not duplicated")
                        elif jid in video_jobs:
                            job = video_jobs[jid]
                            if job.status == "processing":
                                _fail_media_job(job, "Worker heartbeat lost; generation was not duplicated")
                # The old process may still be computing during a network
                # partition. Keep its slot until a result or process restart.
            else:
                m.alive = True
                if not was_alive:
                    print(f"[coordinator] {m.hostname} LIVE — heartbeat recent ({m.last_seen_ago:.0f}s ago)")

IMAGE_JOB_STALE_SEC   = 600   # 10 min — image generation rarely exceeds this
VIDEO_JOB_STALE_SEC   = 7200  # 2 hours — covers first-time model download + inference
IMAGE_JOB_TIMEOUT     = 600   # max wait for synchronous image response

async def _media_requeue_loop():
    """Fail expired media work without releasing a potentially running GPU process."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for job in list(image_jobs.values()):
            if (job.status == "processing"
                    and job.dequeued_at
                    and now - job.dequeued_at > IMAGE_JOB_STALE_SEC):
                _fail_media_job(job, "Image generation exceeded its processing deadline")
        for job in list(video_jobs.values()):
            if (job.status == "processing"
                    and job.dequeued_at
                    and now - job.dequeued_at > VIDEO_JOB_STALE_SEC):
                _fail_media_job(job, "Video generation exceeded its processing deadline")

@app.on_event("startup")
async def _startup():
    _init_db()
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(VIDEO_DIR, exist_ok=True)
    os.makedirs(CONTAINER_ARTIFACT_DIR, exist_ok=True)
    machines.update(_load_machines())
    if machines:
        print(f"[coordinator] Loaded {len(machines)} machine(s) from DB (all marked dead until heartbeat)")
    try:
        restored = restore_quic_handoff(Path(DB_PATH).parent / "rpc-handoff.json", machines,
                                       rpc_clusters, agent_rpc_assignments, RpcCluster)
        if restored:
            print(f"[rpc] restored {len(restored)} idle QUIC cluster(s) from deployment handoff")
    except Exception as exc:
        print(f"[rpc] rejected deployment handoff ({type(exc).__name__}); agents will tear down normally")

    # Restore pending media jobs from DB so they survive coordinator restarts
    loaded_imgs, loaded_vids = _load_media_jobs()
    image_jobs.update(loaded_imgs)
    video_jobs.update(loaded_vids)
    for job in loaded_imgs.values():
        if job.model not in image_job_queues:
            image_job_queues[job.model] = asyncio.Queue()
        await image_job_queues[job.model].put(job)
        print(f"[media] startup: restored image job {job.id[:8]} ({job.model})")
    for job in loaded_vids.values():
        if job.model not in video_job_queues:
            video_job_queues[job.model] = asyncio.Queue()
        await video_job_queues[job.model].put(job)
        print(f"[media] startup: restored video job {job.id[:8]} ({job.model})")

    asyncio.create_task(_health_loop())
    asyncio.create_task(_media_requeue_loop())
    asyncio.create_task(_agent_update_loop())
    asyncio.create_task(_rpc_lifecycle_loop())
    asyncio.create_task(_rpc_recovery_loop())

# ── health (no auth) ──────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

# ── agent endpoints ───────────────────────────────────────────────────────────

@app.post("/agent-status")
async def agent_status(request: Request, payload: AgentStatusPayload, _key: str = Depends(require_agent)):
    ip = _real_ip(request)
    pkey = payload.agent_id or ip
    # Record errors before handling transient status. Restarts and registration
    # must never erase the evidence, including for already registered agents.
    if payload.phase == "ERROR" or payload.detail.startswith(("CRASH:", "Pull failed")):
        report = payload.model_dump()
        report["report_id"] = payload.report_id or str(uuid.uuid4())
        report["agent_id"] = pkey
        report["received_at"] = time.time()
        encoded = _redact_agent_report(json.dumps(report))
        if len(encoded.encode("utf-8")) > 65536:
            raise HTTPException(413, "Agent report exceeds 64 KiB")
        with _db() as con:
            con.execute("""INSERT OR IGNORE INTO agent_reports
                (report_id, agent_id, hostname, model, received_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (report["report_id"], pkey, payload.hostname, payload.model,
                 report["received_at"], encoded))
            con.execute("DELETE FROM agent_reports WHERE id NOT IN (SELECT id FROM agent_reports ORDER BY id DESC LIMIT 1000)")
    if pkey in machines:
        if payload.report_id:
            # Durable reports can arrive hours after a healthy process restart.
            # Archive them above; only live status messages drive availability.
            return {"status": "ok"}
        machines[pkey].phase = payload.phase
        machines[pkey].detail = _redact_agent_report(payload.detail)[:4096]
        return {"status": "ok"}
    if pkey not in pending:
        pending[pkey] = PendingAgent(ip, payload.hostname, payload.model)
    p = pending[pkey]
    p.phase      = payload.phase
    p.detail     = _redact_agent_report(payload.detail)
    p.updated_at = time.time()
    if payload.hostname: p.hostname = payload.hostname
    if payload.model:    p.model    = payload.model
    print(f"[coordinator] {p.hostname} ({pkey}) → {payload.phase} {p.detail}")
    return {"status": "ok"}


def _redact_agent_report(text: str) -> str:
    for secret in (ADMIN_KEY, AGENT_KEY, CONSUMER_KEY):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


@app.get("/agent-reports")
async def get_agent_reports(agent_id: Optional[str] = None, hostname: Optional[str] = None,
                            limit: int = 50, _key: str = Depends(require_admin)):
    clauses, args = [], []
    if agent_id:
        clauses.append("agent_id = ?")
        args.append(agent_id)
    if hostname:
        clauses.append("hostname = ?")
        args.append(hostname)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _db() as con:
        rows = con.execute("SELECT payload_json FROM agent_reports" + where +
                           " ORDER BY id DESC LIMIT ?", args + [max(1, min(limit, 100))]).fetchall()
    return {"reports": [json.loads(row["payload_json"]) for row in rows]}

@app.post("/register")
async def register(request: Request, payload: RegisterPayload, _key: str = Depends(require_agent)):
    ip = _real_ip(request)
    _register_rl.check(ip)

    # Prefer agent_id (stable UUID from agent) over ip:port so machines behind
    # the same NAT don't overwrite each other.
    key = payload.agent_id or f"{ip}:{payload.port}"
    for other in machines.values():
        if (other.agent_id != key and other.alive and other.last_seen_ago <= DEAD_AFTER_SEC
                and other.ip == ip and (payload.lan_ip or payload.hostname) == (other.lan_ip or other.hostname)):
            raise HTTPException(409, "This host already has a live agent; reuse its durable agent ID")

    if key in machines:
        m = machines[key]
        if m.alive and m.last_seen_ago <= DEAD_AFTER_SEC and payload.hostname and payload.hostname != m.hostname:
            raise HTTPException(409, "Agent ID is already in use by another live hostname; regenerate the cloned agent ID")
        previous_pid = (m.runtime or {}).get("agent_pid")
        restarted = payload.agent_pid is not None and previous_pid is not None and payload.agent_pid != previous_pid
        if restarted:
            _requeue_chat_from(m)
            m.busy = False
            m.busy_job_id = None
            m.busy_claim_token = None
            m.busy_claim_request_id = None
            m.busy_chat_model = None
            m.media_phase = None
            m.media_model = None
            if m.rpc_cluster_id in rpc_clusters:
                _dissolve_cluster(rpc_clusters[m.rpc_cluster_id], "agent_restarted")
        m.last_heartbeat = time.time()
        m.alive            = True
        # A model pull also re-registers. It must not erase a live workload slot.
        m.ip, m.port = ip, payload.port
        m.model  = payload.model
        if payload.hostname:       m.hostname       = payload.hostname
        if payload.cpu_name:       m.cpu_name       = payload.cpu_name
        if payload.cpu_cores:      m.cpu_cores      = payload.cpu_cores
        if payload.ram_total_gb:   m.ram_total_gb   = payload.ram_total_gb
        if payload.ram_free_gb:    m.ram_free_gb     = payload.ram_free_gb
        if payload.gpu_name:       m.gpu_name       = payload.gpu_name
        if "gpu_vram_gb" in payload.model_fields_set:
            m.gpu_vram_gb = payload.gpu_vram_gb
        if payload.network_mbps:   m.network_mbps   = payload.network_mbps
        if payload.ollama_version: m.ollama_version = payload.ollama_version
        if payload.os_version:     m.os_version     = payload.os_version
        if payload.lan_ip:         m.lan_ip         = payload.lan_ip
        print(f"[coordinator] {m.hostname} ({key}) re-registered")
    else:
        machines[key] = MachineState(ip, payload)
        print(f"[coordinator] {machines[key].hostname} ({key}) NEW  model={payload.model}  gpu={payload.gpu_name or 'CPU-only'}")

    if payload.agent_pid is not None:
        machines[key].runtime = dict(machines[key].runtime or {}, agent_pid=payload.agent_pid)
    if not machines[key].busy:
        machines[key].phase = "RUNNING"

    _upsert_machine(machines[key])
    pending.pop(payload.agent_id or ip, None)
    pending.pop(ip, None)
    return {"status": "ok", "key": key, "machines_total": len(machines)}

@app.post("/heartbeat")
async def heartbeat(request: Request, payload: HeartbeatPayload, _key: str = Depends(require_agent)):
    ip  = _real_ip(request)
    key = payload.agent_id or f"{ip}:{payload.port}"
    if key not in machines:
        raise HTTPException(404, "Not registered — run /register first")
    previous_pid = (machines[key].runtime or {}).get("agent_pid")
    if payload.agent_pid is not None and previous_pid is not None and payload.agent_pid != previous_pid:
        # Registration owns restart handling. Do not erase its comparison PID
        # when an early heartbeat wins the startup race.
        raise HTTPException(404, "Agent process changed; re-register before heartbeating")
    if payload.agent_revision and machines[key].gpu_vram_gb and not machines[key].lan_ip:
        # Old persisted rows omitted LAN addresses. Existing Python workers
        # already re-register on a 404 heartbeat, which refreshes this safely.
        raise HTTPException(404, "Re-register to refresh the private LAN address")
    machines[key].last_heartbeat = time.time()
    machines[key].alive = True
    if (payload.active_job_id and not machines[key].busy
            and payload.active_job_id not in machines[key].completed_job_ids
            and (payload.active_job_id, payload.active_claim_token) not in terminal_chat_claims):
        # A restarted coordinator has no in-memory chat lease. Preserve the
        # physical worker slot until its old computation really finishes.
        machines[key].busy = True
        machines[key].busy_job_id = payload.active_job_id
        machines[key].busy_claim_token = payload.active_claim_token
    # An idle snapshot may have been collected before a new claim, then delayed
    # in transit. Only a matching result acknowledgement or process restart can
    # release the physical slot; heartbeat absence is not a completion signal.
    if payload.agent_pid is not None:
        machines[key].runtime = {
            "agent_pid": payload.agent_pid, "agent_revision": payload.agent_revision,
            "supervised": payload.supervised,
            "ollama_models": payload.ollama_models,
            "rpc_log": payload.rpc_log.model_dump() if payload.rpc_log else None,
            "ollama_pid": payload.ollama_pid, "ollama_ready": payload.ollama_ready,
            "rpc_pid": payload.rpc_pid, "gpu_used_mb": payload.gpu_used_mb,
            "rpc_relay_external": payload.rpc_relay_external,
            "rpc_quic": payload.rpc_quic, "rpc_preflight": payload.rpc_preflight,
            "rpc_tuning": payload.rpc_tuning, "rpc_stream": payload.rpc_stream,
            "rpc_gpu_layers": payload.rpc_gpu_layers, "rpc_total_layers": payload.rpc_total_layers,
        }
    _upsert_machine(machines[key])
    return {"status": "ok"}

# ── RPC cluster agent endpoints ───────────────────────────────────────────────

@app.get("/agent/rpc/assignment")
async def get_rpc_assignment(request: Request, _key: str = Depends(require_agent)):
    """Agents poll this to discover their RPC role. Returns 204 if no assignment."""
    ip = _real_ip(request)
    # Try to identify caller by agent_id query param, then fall back to ip
    agent_id = request.query_params.get("agent_id") or ip
    assignment = agent_rpc_assignments.get(agent_id)
    if not assignment and not request.query_params.get("agent_id"):
        # Try matching by ip (agent may not supply agent_id on first poll)
        for key, m in machines.items():
            if m.ip == ip:
                assignment = agent_rpc_assignments.get(key)
                if assignment:
                    agent_id = key
                    break
    if not assignment:
        return Response(status_code=204)
    cluster = rpc_clusters.get(assignment["cluster_id"])
    if cluster and cluster.relay_url and not cluster.relay_ready and cluster.status != "dissolving":
        return Response(status_code=204)
    if assignment.get("role") == "worker" and assignment.get("transport") == "relay":
        if cluster and cluster.relay_url and cluster.status != "dissolving":
            try:
                remote = await asyncio.to_thread(_regional_relay_call, cluster, "GET")
            except Exception:
                raise HTTPException(503, "Relay is temporarily unavailable")
            if agent_rpc_assignments.get(agent_id) is not assignment:
                return Response(status_code=204)
            assignment = dict(assignment, relay_connections=remote["connections"].get(agent_id, []))
        else:
            assignment = dict(assignment, relay_connections=[
                identity[2] for identity, connection in rpc_relays.items()
                if identity[:2] == (assignment["cluster_id"], agent_id) and not connection.closed.is_set()])
    return assignment


@app.post("/agent/rpc/preflight")
async def rpc_preflight(payload: RpcPreflightPayload, _key: str = Depends(require_agent)):
    cluster = rpc_clusters.get(payload.cluster_id)
    assignment = agent_rpc_assignments.get(payload.agent_id)
    if (not cluster or cluster.status not in ("forming", "active") or not assignment
            or assignment.get("cluster_id") != payload.cluster_id or assignment.get("role") != payload.role
            or not assignment.get("preflight_required")):
        raise HTTPException(409, "Route check does not match a current assignment")
    if payload.stage not in ("checking", "verified", "native_ready", "failed", "runtime_failed"):
        raise HTTPException(422, "Invalid route-check stage")
    stages = {"checking": 0, "verified": 1, "native_ready": 2}
    previous = cluster.preflight.get(payload.agent_id)
    if payload.stage in stages and stages[payload.stage] < stages.get(previous, -1):
        raise HTTPException(409, "Route-check stage cannot go backwards")
    if payload.stage == "native_ready" and (payload.role != "worker" or previous not in ("verified", "native_ready")):
        raise HTTPException(409, "Worker must verify the route before native startup")
    cluster.preflight[payload.agent_id] = payload.stage
    if payload.stage in ("failed", "runtime_failed"):
        _dissolve_cluster(cluster, "preflight_failed" if payload.stage == "failed" else "inference_failed")
    return {"workers_ready": all(cluster.preflight.get(aid) == "native_ready" for aid in cluster.worker_ids)}


@app.post("/agent/rpc/ready")
async def rpc_ready(payload: RpcReadyPayload, _key: str = Depends(require_agent)):
    """Primary agent calls this once llama-server is up and ready to accept jobs."""
    cluster = rpc_clusters.get(payload.cluster_id)
    if not cluster:
        raise HTTPException(404, "Cluster not found")
    assignment = agent_rpc_assignments.get(payload.agent_id)
    if cluster.status not in ("forming", "active") or not assignment or \
            assignment.get("cluster_id") != payload.cluster_id or assignment.get("role") != payload.role:
        raise HTTPException(409, "RPC readiness does not match a current assignment")
    if payload.role == "primary":
        if payload.agent_id != cluster.primary_id:
            raise HTTPException(403, "RPC readiness must come from the assigned primary")
        if assignment.get("preflight_required") and (cluster.preflight.get(payload.agent_id) != "verified"
                or any(cluster.preflight.get(aid) != "native_ready" for aid in cluster.worker_ids)):
            raise HTTPException(409, "Route checks must pass before model readiness")
        cluster.primary_ready = True
        cluster.status        = "active"
        print(f"[rpc] cluster {payload.cluster_id[:8]} ACTIVE — primary {payload.agent_id} ready")
    return {"status": "ok"}

@app.post("/agent/rpc/done")
async def rpc_done(payload: RpcReadyPayload, _key: str = Depends(require_agent)):
    """Agent calls this after it has finished tearing down its RPC role."""
    assignment = agent_rpc_assignments.get(payload.agent_id)
    if not assignment or assignment["cluster_id"] != payload.cluster_id or assignment.get("role") != payload.role:
        return {"status": "ok", "stale": True}
    agent_rpc_assignments.pop(payload.agent_id, None)
    m = machines.get(payload.agent_id)
    # If the primary reports done while cluster is still forming/active (e.g. GGUF
    # missing), dissolve the cluster immediately so workers aren't stranded.
    cluster = rpc_clusters.get(payload.cluster_id)
    if cluster and cluster.status in ("forming", "active"):
        _dissolve_cluster(cluster, f"{payload.role}_aborted")
    if m and m.rpc_cluster_id == payload.cluster_id:
        # This matching acknowledgment confirms native teardown, even when its
        # final result upload was lost. Heartbeat silence cannot confirm this.
        if payload.role == "primary" and m.busy_job_id:
            _release_chat_slot(m.busy_job_id, m.busy_claim_token)
        m.in_rpc = False
        m.rpc_cluster_id = None
    return {"status": "ok"}


@app.post("/rpc/clusters/{cluster_id}/stop")
async def stop_idle_rpc_cluster(cluster_id: str, _key: str = Depends(require_admin)):
    """Release a verified idle cluster for tuning without cancelling user work."""
    cluster = rpc_clusters.get(cluster_id)
    if not cluster:
        raise HTTPException(404, "Cluster not found")
    members = [cluster.primary_id, *cluster.worker_ids]
    if cluster.status == "dissolving":
        return {"status": "dissolving"}
    if (cluster.status != "active" or not cluster.primary_ready
            or cluster_id in active_chat_streams.values()
            or any(machines.get(aid) and machines[aid].busy for aid in members)
            or any(not job.done_event.is_set() and not job.cancelled
                   and (job.rpc_cluster_id == cluster_id or
                        (job.rpc_cluster_id is None and job.model in _RPC_MODELS and job.model == cluster.model))
                   for job in jobs.values())):
        raise HTTPException(409, "Cluster has active or queued work; retry when idle")
    _dissolve_cluster(cluster, "admin_idle")
    return {"status": "dissolving"}

install_relay_routes(app, rpc_clusters, rpc_relays, require_agent)
install_quic_routes(app, rpc_clusters, agent_rpc_assignments, machines, require_agent)


# ── generic container jobs ───────────────────────────────────────────────────

@app.post("/container/jobs", status_code=201)
async def create_container_job(payload: ContainerJobCreate, _key: str = Depends(require_admin)):
    """Queue a constrained OCI image for execution by any compatible container worker."""
    _validate_container_job(payload)
    job_id = str(uuid.uuid4())
    now = time.time()
    with _db() as con:
        con.execute("""
            INSERT INTO container_jobs
            (id, image, command_json, env_json, artifacts_json, timeout_sec, cpus,
             memory_mb, network, platform, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
        """, (job_id, payload.image, json.dumps(payload.command), json.dumps(payload.env),
              json.dumps(payload.artifacts), payload.timeout_sec, payload.cpus,
              payload.memory_mb, int(payload.network), payload.platform, now))
        row = con.execute("SELECT * FROM container_jobs WHERE id=?", (job_id,)).fetchone()
    return _container_job_dict(row)

@app.get("/container/jobs")
async def list_container_jobs(limit: int = 100, _key: str = Depends(require_admin)):
    limit = max(1, min(limit, 500))
    with _db() as con:
        rows = con.execute(
            "SELECT * FROM container_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"jobs": [_container_job_dict(row) for row in rows]}

@app.get("/container/jobs/{job_id}")
async def get_container_job(job_id: str, _key: str = Depends(require_admin)):
    with _db() as con:
        row = con.execute("SELECT * FROM container_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Container job not found")
    return _container_job_dict(row)

@app.get("/container/jobs/{job_id}/artifacts")
async def download_container_artifacts(job_id: str, _key: str = Depends(require_admin)):
    with _db() as con:
        row = con.execute("SELECT artifact_path FROM container_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Container job not found")
    if not row["artifact_path"] or not os.path.isfile(row["artifact_path"]):
        raise HTTPException(404, "This job has no artifact archive")
    return FileResponse(row["artifact_path"], media_type="application/gzip",
                        filename=f"{job_id}-artifacts.tar.gz")

@app.get("/agent/container/jobs/next")
async def next_container_job(agent_id: str, platform: Optional[str] = None,
                             _key: str = Depends(require_agent)):
    if agent_id not in machines or not machines[agent_id].alive:
        raise HTTPException(409, "Container worker must register before polling")
    machine = machines[agent_id]
    if not _worker_available(machine):
        return Response(status_code=204)
    now = time.time()
    with _db() as con:
        con.execute("BEGIN IMMEDIATE")
        # Expired leases are safe to retry because each attempt gets a fresh container.
        con.execute("""
            UPDATE container_jobs SET status='queued', agent_id=NULL, lease_until=NULL,
                started_at=NULL WHERE status='running' AND lease_until < ?
        """, (now,))
        row = con.execute("""
            SELECT * FROM container_jobs
            WHERE status='queued' AND (platform IS NULL OR platform=?)
                AND cpus <= ? AND memory_mb <= ?
            ORDER BY created_at LIMIT 1
        """, (platform, machine.cpu_cores or 0,
              int((machine.ram_free_gb if machine.ram_free_gb is not None else machine.ram_total_gb or 0) * 1024))).fetchone()
        if not row:
            con.commit()
            return Response(status_code=204)
        changed = con.execute("""
            UPDATE container_jobs SET status='running', agent_id=?, started_at=?, lease_until=?
            WHERE id=? AND status='queued'
        """, (agent_id, now, now + max(CONTAINER_LEASE_SEC, row["timeout_sec"] + 3600),
              row["id"])).rowcount
        if not changed:
            con.commit()
            return Response(status_code=204)
        con.commit()
        claimed = con.execute("SELECT * FROM container_jobs WHERE id=?", (row["id"],)).fetchone()
    machines[agent_id].busy = True
    machines[agent_id].busy_job_id = row["id"]
    return _container_job_dict(claimed, include_private=True)

@app.post("/agent/container/jobs/{job_id}/artifacts")
async def upload_container_artifacts(job_id: str, request: Request, agent_id: str,
                                     _key: str = Depends(require_agent)):
    with _db() as con:
        row = con.execute("SELECT status, agent_id FROM container_jobs WHERE id=?", (job_id,)).fetchone()
    if not row or row["status"] != "running" or row["agent_id"] != agent_id:
        raise HTTPException(409, "Job is not leased to this worker")
    content_length = int(request.headers.get("content-length", "0") or 0)
    if content_length > MAX_ARTIFACT_BYTES:
        raise HTTPException(413, "Artifact archive is too large")
    final_path = os.path.join(CONTAINER_ARTIFACT_DIR, f"{job_id}.tar.gz")
    temp_path = final_path + ".part"
    written = 0
    try:
        with open(temp_path, "wb") as output:
            async for chunk in request.stream():
                written += len(chunk)
                if written > MAX_ARTIFACT_BYTES:
                    raise HTTPException(413, "Artifact archive is too large")
                output.write(chunk)
        os.replace(temp_path, final_path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
    with _db() as con:
        con.execute("UPDATE container_jobs SET artifact_path=? WHERE id=?", (final_path, job_id))
    return {"status": "ok", "bytes": written}

@app.post("/agent/container/jobs/{job_id}/result")
async def finish_container_job(job_id: str, payload: ContainerJobResult, agent_id: str,
                               _key: str = Depends(require_agent)):
    now = time.time()
    status = "completed" if payload.exit_code == 0 and not payload.error else "failed"
    with _db() as con:
        changed = con.execute("""
            UPDATE container_jobs SET status=?, finished_at=?, elapsed_ms=?, exit_code=?,
                stdout=?, stderr=?, error=?, lease_until=NULL
            WHERE id=? AND status='running' AND agent_id=?
        """, (status, now, payload.elapsed_ms, payload.exit_code,
              payload.stdout[-65536:], payload.stderr[-65536:], payload.error,
              job_id, agent_id)).rowcount
    if not changed:
        raise HTTPException(409, "Job is not leased to this worker")
    if agent_id in machines and machines[agent_id].busy_job_id == job_id:
        machines[agent_id].busy = False
        machines[agent_id].busy_job_id = None
    return {"status": status}

# ── agent job endpoints ───────────────────────────────────────────────────────

@app.get("/agent/jobs/queued-models")
async def queued_models(agent_id: Optional[str] = None, _key: str = Depends(require_agent)):
    """Return list of model IDs that currently have at least one pending job.
    Agents use this to discover demand dynamically instead of polling a fixed catalog."""
    machine = machines.get(agent_id)
    if agent_id and (not machine or not _worker_available(machine)):
        return {"models": []}
    waiting = sorted(_queued_chat_jobs(), key=lambda job: job.created_at)
    return {"models": list(dict.fromkeys(job.model for job in waiting
        if job.model not in _RPC_MODELS and not job.rpc_cluster_id and not job.rpc_recovering
        and (not machine or _can_serve_single(machine, job.model, job.body.get("options", {}).get("num_ctx")))))}

@app.get("/agent/jobs/peek")
async def peek_next_job(request: Request, model: str, _key: str = Depends(require_agent)):
    """Check if a job is waiting for this model WITHOUT consuming it. Returns 204 if none.
    Used by agents to detect demand before downloading a model on-demand."""
    if any(not job.rpc_cluster_id and not job.rpc_recovering and model not in _RPC_MODELS
           for job in _queued_chat_jobs(model)):
        return Response(status_code=200)
    return Response(status_code=204)

@app.get("/agent/jobs/next")
async def get_next_job(request: Request, model: str, agent_id: Optional[str] = None,
                       claim_request_id: Optional[str] = Query(default=None, min_length=1, max_length=64),
                       _key: str = Depends(require_agent)):
    """Agent polls this to get the next pending job for its model. Returns 204 if none."""
    machine = machines.get(agent_id)
    if not machine or not machine.alive:
        raise HTTPException(409, "A registered agent_id is required to claim work")
    if (machine.busy and claim_request_id and machine.busy_claim_request_id == claim_request_id
            and machine.busy_chat_model == model):
        job = jobs.get(machine.busy_job_id)
        valid = bool(job and not job.cancelled and not job.done_event.is_set()
                     and job.assigned_agent_id == agent_id and job.claim_token == machine.busy_claim_token)
        if valid and (model in _RPC_MODELS or job.rpc_cluster_id):
            cluster = _rpc_cluster_for_machine(machine, model)
            valid = bool(cluster and cluster.status == "active" and cluster.primary_id == agent_id
                         and _job_matches_rpc_cluster(job, cluster)
                         and all(aid in machines and machines[aid].alive and machines[aid].last_seen_ago <= DEAD_AFTER_SEC
                                 for aid in [cluster.primary_id, *cluster.worker_ids]))
        elif valid:
            valid = _can_serve_single(machine, model, job.body.get("options", {}).get("num_ctx"))
        if valid:
            return {"id": job.id, "body_json": json.dumps(job.body), "claim_token": job.claim_token}
        # The caller still does not know its offer's identity. Return a fenced
        # receipt so it can acknowledge cancellation without starting inference.
        return {"id": machine.busy_job_id, "claim_token": machine.busy_claim_token,
                "body_json": "{}", "cancelled": True}
    cluster = _rpc_cluster_for_machine(machine, model)
    rpc_claim = model in _RPC_MODELS or cluster is not None
    if rpc_claim:
        if (not cluster or cluster.primary_id != agent_id or not cluster.primary_ready
                or cluster.status != "active" or machine.busy or any(
                    member not in machines or not machines[member].alive
                    or machines[member].last_seen_ago > DEAD_AFTER_SEC
                    for member in [cluster.primary_id, *cluster.worker_ids])):
            return Response(status_code=204)
    elif not _worker_available(machine) or not _can_serve_single(machine, model):
        return Response(status_code=204)
    q = job_queues.get(model)
    if q:
        for _ in range(q.qsize()):
            try:
                job = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if job.cancelled or job.done_event.is_set() or job.assigned_agent_id is not None:
                print(f"[coordinator] Draining cancelled job {job.id[:8]} from queue ({job.model})")
                continue
            if not rpc_claim and (job.rpc_cluster_id or job.rpc_recovering
                    or not _can_serve_single(machine, model, job.body.get("options", {}).get("num_ctx"))):
                q.put_nowait(job)
                continue
            if rpc_claim:
                if not _job_matches_rpc_cluster(job, cluster):
                    q.put_nowait(job)
                    continue
                job.rpc_cluster_id = cluster.cluster_id
            job.assigned_agent_id = agent_id
            job.attempts += 1
            job.claim_token = str(uuid.uuid4())
            machine.busy = True
            machine.busy_job_id = job.id
            machine.busy_claim_token = job.claim_token
            machine.busy_claim_attempt = job.attempts
            machine.busy_claim_request_id = claim_request_id
            machine.busy_chat_model = model
            # Send body as a pre-serialized JSON string so the agent can
            # forward it to Ollama verbatim — avoids PS 5.1 ConvertTo-Json
            # corrupting the body on the ConvertFrom-Json → ConvertTo-Json
            # round-trip (same issue we fixed for results).
            return {"id": job.id, "body_json": json.dumps(job.body), "claim_token": job.claim_token}
    return Response(status_code=204)

@app.get("/agent/jobs/{job_id}/cancelled")
async def check_job_cancelled(job_id: str, _key: str = Depends(require_agent)):
    """Agents poll this mid-execution to learn if the consumer cancelled the request.
    Returns {cancelled: true} when the job is gone (timed out, disconnected, or done)."""
    job = jobs.get(job_id)
    if not job or job.cancelled:
        return {"cancelled": True}
    return {"cancelled": False}


install_stream_routes(app, jobs, require_agent)

@app.post("/agent/jobs/{job_id}/result")
async def submit_job_result(
    job_id: str, payload: JobResultPayload, _key: str = Depends(require_agent)
):
    job = jobs.get(job_id)
    if not job:
        _remember_terminal_claim(job_id, payload.claim_token)
        _release_chat_slot(job_id, payload.claim_token)
        raise HTTPException(404, "Job not found or already completed")
    if job.attempts and (not job.assigned_agent_id or
            (payload.claim_token != job.claim_token and not (payload.claim_token is None and job.attempts == 1))):
        _remember_terminal_claim(job_id, payload.claim_token)
        _release_chat_slot(job_id, payload.claim_token)
        raise HTTPException(409, "Result belongs to an expired job claim")

    if getattr(job, "rpc_recovering", False):
        raise HTTPException(409, "Result belongs to a recovering RPC session")
    if payload.error and job.rpc_cluster_id and (payload.upstream_status is None or
                                               payload.upstream_status in (429, 502, 503, 504)):
        cluster = rpc_clusters.get(job.rpc_cluster_id)
        if cluster and cluster.automatic and cluster.status in ("forming", "active"):
            _dissolve_cluster(cluster, "inference_failed")
            return {"status": "retrying"}

    result = payload.result
    # Agents call Ollama's /api/chat (native) instead of /v1/chat/completions so
    # that options.num_ctx is respected. Convert native format → OpenAI format here.
    if result and "done_reason" in result and "choices" not in result:
        msg        = result.get("message", {})
        tool_calls = msg.get("tool_calls") or None

        # Native API may return tool_call arguments as a dict; OpenAI needs a JSON string.
        # Also inject id/type fields that goose may expect.
        if tool_calls:
            for tc in tool_calls:
                fn = tc.get("function", {})
                if isinstance(fn.get("arguments"), dict):
                    fn["arguments"] = json.dumps(fn["arguments"])
                tc.setdefault("id",   f"call_{uuid.uuid4().hex[:8]}")
                tc.setdefault("type", "function")

        done_reason   = result.get("done_reason") or "stop"
        finish_reason = "tool_calls" if tool_calls else done_reason

        message: dict = {"role": msg.get("role", "assistant"), "content": msg.get("content")}
        if msg.get("thinking"):
            message["reasoning_content"] = msg["thinking"]
        if tool_calls:
            message["tool_calls"] = tool_calls

        result = {
            "id":      f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object":  "chat.completion",
            "created": int(time.time()),
            "model":   result.get("model", ""),
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens":     result.get("prompt_eval_count") or 0,
                "completion_tokens": result.get("eval_count") or 0,
                "total_tokens":      (result.get("prompt_eval_count") or 0) + (result.get("eval_count") or 0),
            },
        }

    # Don't let a retry error-report overwrite a successful result.
    # WinError 10054 causes agents to see a connection reset after the
    # coordinator already sent 200 OK.  The agent then retries and/or
    # posts {"error": "ConnectionResetError..."}, which must not clobber
    # the real result that was already stored.
    if job.done_event.is_set():
        _remember_terminal_claim(job_id, job.claim_token)
        return {"status": "ok"}  # idempotent — result already committed

    job.result     = result
    job.error      = payload.error
    job.upstream_status = payload.upstream_status
    job.elapsed_ms = payload.elapsed_ms
    job.done_event.set()
    _remember_terminal_claim(job_id, job.claim_token)
    _release_chat_slot(job_id, job.claim_token)
    return {"status": "ok"}

# ── admin endpoints ───────────────────────────────────────────────────────────

install_admin_routes(app, require_admin=require_admin, repository=repository,
                     machines=machines, pending=pending, rpc_clusters=rpc_clusters,
                     active_chat_streams=active_chat_streams, queued_jobs=_queued_chat_jobs)


# ── consumer endpoints ────────────────────────────────────────────────────────

@app.get("/v1/models")
async def list_models(_key: str = Depends(require_consumer)):
    seen: dict[str, Any] = {}
    for m in machines.values():
        if m.alive and not m.in_rpc and _can_serve_single(m, m.model) and m.model not in seen:
            seen[m.model] = {
                "id": m.model, "object": "model",
                "created": int(m.registered_at), "owned_by": "peerfoundry",
            }
    # Include RPC models that have enough idle GPU capacity to form a cluster
    for rpc_model, req_vram in {**_RPC_MODELS, **RPC_FALLBACK_MODELS}.items():
        if rpc_model not in seen:
            if _on_demand_capacity(rpc_model):
                continue  # Publish the ordinary path below when one GPU can fit it.
            if _select_rpc_machines(req_vram) or _find_rpc_cluster_for_model(rpc_model):
                seen[rpc_model] = {
                    "id": rpc_model, "object": "model",
                    "created": int(time.time()), "owned_by": "peerfoundry-rpc",
                }
    # Advertise only bounded on-demand models that at least one live worker can
    # actually load. The first request creates the queue observed by workers.
    for model in _ON_DEMAND_MODELS:
        if model not in seen and _on_demand_capacity(model):
            seen[model] = {
                "id": model, "object": "model",
                "created": int(time.time()), "owned_by": "peerfoundry-on-demand",
            }
    return {"object": "list", "data": list(seen.values())}

async def _stream_chat_response(job, request, user_id, t_start, include_usage):
    active_chat_streams[job.id] = job.rpc_cluster_id
    completion = asyncio.create_task(_await_chat_completion(job, request, user_id, t_start))
    reader = None
    try:
        while True:
            if completion.done():
                while not job.live_stream.queue.empty():
                    for chunk in job.live_stream.queue.get_nowait():
                        yield sse(chunk)
                break
            reader = asyncio.create_task(job.live_stream.queue.get())
            done, _ = await asyncio.wait((completion, reader), timeout=10, return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                for chunk in reader.result():
                    yield sse(chunk)
            else:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                if not done:
                    yield ": keep-alive\n\n"
        if not completion.result():
            return
        result = _finalize_chat_response(job, user_id, t_start)
        choice = (result.get("choices") or [{}])[0]
        yield sse({"id": "chatcmpl-" + job.id, "object": "chat.completion.chunk",
            "created": int(job.created_at), "model": job.model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}]})
        if include_usage and result.get("usage"):
            yield sse({"id": "chatcmpl-" + job.id, "object": "chat.completion.chunk",
                "created": int(job.created_at), "model": job.model, "choices": [], "usage": result["usage"]})
        yield "data: [DONE]\n\n"
    except HTTPException as exc:
        yield sse({"error": {"message": str(exc.detail), "type": "upstream_error", "code": exc.status_code}})
        yield "data: [DONE]\n\n"
    finally:
        # Synchronous fencing must precede awaits: ASGI cancellation scopes can
        # cancel every cleanup checkpoint after a consumer disconnects.
        if not job.done_event.is_set():
            _cancel_job(job.id, "stream_disconnected")
        jobs.pop(job.id, None)
        active_chat_streams.pop(job.id, None)
        if reader and not reader.done():
            reader.cancel()
        completion.cancel()
        await asyncio.gather(*([reader] if reader else []), completion, return_exceptions=True)


def _finalize_chat_response(job, user_id, t_start):
    requested_model = job.model
    jobs.pop(job.id, None)
    machine = machines.get(job.assigned_agent_id)

    if job.error:
        if machine:
            machine.requests_err += 1
            _upsert_machine(machine)
        _log_usage(requested_model, user_id, 0, 0, job.elapsed_ms, False,
                   finish_reason="error",
                   hostname=machine.hostname if machine else None)
        print(f"[job {job.id[:8]}] upstream request failed | status={job.upstream_status}")
        status = job.upstream_status if job.upstream_status in (400, 413, 422) else 502
        raise HTTPException(status, f"Upstream error: {job.error[:300]}")

    elapsed = (time.time() - t_start) * 1000
    result_raw       = job.result or {}
    finish_reason    = (result_raw.get("choices") or [{}])[0].get("finish_reason", "?")
    sent_ctx         = job.body.get("options", {}).get("num_ctx", "?")
    sent_predict     = job.body.get("options", {}).get("num_predict", "?")
    usage            = result_raw.get("usage", {})
    prompt_toks      = usage.get("prompt_tokens", "?")
    completion_toks  = usage.get("completion_tokens", "?")
    print(f"[job {job.id[:8]}] finish={finish_reason}  ctx_sent={sent_ctx}  num_predict={sent_predict}  prompt={prompt_toks}  completion={completion_toks}")
    # Update stats on whichever machine actually served the job (may differ from initial pick)
    serving_machine = machine
    if serving_machine:
        serving_machine.requests_ok += 1
        serving_machine.response_times.append(job.elapsed_ms or elapsed)
        _upsert_machine(serving_machine)
    _log_usage(
        requested_model, user_id,
        usage.get("prompt_tokens") or 0,
        usage.get("completion_tokens") or 0,
        job.elapsed_ms or elapsed,
        True,
        finish_reason  = finish_reason if finish_reason not in (None, "?") else "stop",
        hostname       = serving_machine.hostname if serving_machine else None,
        queue_wait_ms  = max(0.0, elapsed - (job.elapsed_ms or 0)) if job.elapsed_ms else None,
    )

    # Strip non-OpenAI fields from qwen3 thinking mode.
    # - "reasoning" key: Ollama returns thinking as a separate message field
    # - <think>...</think> blocks: sometimes embedded inline in content
    import re as _re
    result = job.result or {}
    for choice in result.get("choices", []):
        msg = choice.get("message", {})
        msg.pop("reasoning", None)
        if isinstance(msg.get("content"), str):
            raw = msg["content"]
            stripped = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
            # Only apply stripping if something remains — if all content was inside
            # <think> blocks the model likely put its real answer there; keep raw.
            msg["content"] = stripped if stripped else raw
            if not stripped and raw:
                print(f"[job {job.id[:8]}] all content was in <think> blocks — keeping raw")

    return result


def _estimated_context_need(messages: list[dict]) -> int:
    char_count = sum(len(str(message.get("content", ""))) for message in messages)
    return char_count // 3 + 8192  # code-aware prompt estimate plus response/tool reserve


def _automatic_context_size(model: str, messages: list[dict]) -> int:
    """Choose a bounded model- and capacity-aware context before placement."""
    estimated_tokens = max(0, _estimated_context_need(messages) - 8192)
    model_lower = model.lower()
    if any(size in model_lower for size in ("24b", "27b", "30b", "32b")):
        context_floor, context_cap = 8192, 32768
    elif "14b" in model_lower:
        context_floor, context_cap = 16384, 32768
    else:
        context_floor, context_cap = 49152, 65536
    # Native RPC startup uses stable power-of-two context tiers. Preserve the
    # model's proven default for ordinary prompts, then move to the next tier
    # when prompt tokens plus an 8K response/tool reserve no longer fit.
    if model in _RPC_MODELS or model in RPC_FALLBACK_MODELS:
        context_floor = max(context_floor, _RPC_DEFAULT_CONTEXT.get(model, 0))
        if model in RPC_FALLBACK_MODELS and estimated_tokens + 8192 <= 16384 and any(
                machine.alive and not machine.in_rpc
                and _can_serve_single(machine, model, 16384)
                for machine in machines.values()):
            context_floor = 16384
        desired = max(context_floor, estimated_tokens + 8192)
        return next((tier for tier in (8192, 16384, 32768) if tier >= desired), context_cap)
    return max(context_floor, min(context_cap, estimated_tokens + 16384))


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, _key: str = Depends(require_consumer)):
    _ip = _real_ip(request)
    _consumer_rl.check(_ip)
    user_id = request.headers.get("X-User-ID") or _ip

    body = await _read_chat_request(request)
    requested_model = MODEL_ALIASES.get(body["model"], body["model"])
    runtime_override = body.pop("rpc_runtime", None)
    if runtime_override is not None:
        if not _ct_match(_extract_key(request, _key), ADMIN_KEY):
            raise HTTPException(403, "RPC tuning requires the admin key")
        try:
            runtime_override = validate_runtime(runtime_override)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if (requested_model not in _RPC_MODELS and requested_model not in RPC_FALLBACK_MODELS
                or body.get("rpc_transport") != "quic" or not body.get("rpc_agent_ids")):
            raise HTTPException(422, "RPC tuning requires an explicitly pinned QUIC pair")
    explicit_context = "num_ctx" in body["options"]
    if not explicit_context:
        body["options"]["num_ctx"] = _automatic_context_size(
            requested_model, body.get("messages", []))
    _check_chat_queue_capacity(requested_model, _chat_payload_bytes(body))
    created_cluster = None

    # RPC models are served by dynamically formed multi-machine clusters, not by
    # individual Ollama agents — skip the "any_registered" check for them.
    _rpc_vram_needed = _rpc_memory_required(requested_model, body["options"].get("num_ctx"),
                                          "rpc_transport" in body or "rpc_agent_ids" in body)
    if _rpc_vram_needed:
        selected_ids = body.get("rpc_agent_ids")
        if selected_ids is not None:
            if not _ct_match(_extract_key(request, _key), ADMIN_KEY):
                raise HTTPException(403, "Explicit trial membership requires the admin key")
            try:
                validate_contract("trial-membership", body)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        cluster = _find_rpc_cluster_for_model(requested_model, agent_ids=selected_ids)
        if cluster and runtime_override is not None and cluster.runtime_profile != runtime_override:
            raise HTTPException(409, "Existing cluster has different runtime settings; stop it when idle first")
        if cluster and body.get("rpc_transport", RPC_DEFAULT_TRANSPORT) == "auto":
            cluster.automatic = True
            if cluster.transport == "quic":
                cluster.route_id = "lan-quic"
        if cluster and body.get("rpc_transport") == "quic" and cluster.transport != "quic":
            raise HTTPException(409, "Existing model cluster uses a different transport; wait for its idle teardown")
        if cluster and cluster.relay_url and cluster.relay_deadline and time.time() >= cluster.relay_deadline - 300:
            # Stop admitting new work onto a connection about to lose its host.
            # Existing requests finish before idle teardown and fallback formation.
            raise HTTPException(503, "Relay is nearing its runtime limit; retry after cluster teardown",
                                headers={"Retry-After": "30"})
        options = body.setdefault("options", {})
        ctx_size = options.get("num_ctx", cluster.ctx_size if cluster else _RPC_DEFAULT_CONTEXT.get(requested_model, 4096))
        if (cluster and not explicit_context
                and _estimated_context_need(body.get("messages", [])) <= cluster.ctx_size):
            ctx_size = cluster.ctx_size
        if type(ctx_size) is not int or not 512 <= ctx_size <= 32768:
            raise HTTPException(422, "RPC num_ctx must be an integer between 512 and 32768")
        if cluster and ctx_size > cluster.ctx_size:
            raise HTTPException(409, f"RPC cluster has {cluster.ctx_size} context tokens; requested {ctx_size}. "
                                     "Retry after the cluster's idle unload to start a larger context.")
        # Keep the queued request consistent with the context fixed at native startup.
        options["num_ctx"] = ctx_size
        if not cluster:
            transport = body.get("rpc_transport", RPC_DEFAULT_TRANSPORT)
            if transport not in ("auto", "direct", "relay", "quic"):
                raise HTTPException(422, "rpc_transport must be auto, direct, relay or quic")
            if transport == "auto":
                _new_cluster = await _form_automatic_rpc_cluster(requested_model, _rpc_vram_needed, ctx_size, selected_ids)
                created_here = True
            elif relay_provisioner and transport == "relay":
                _new_cluster, created_here = await _form_managed_rpc_cluster(requested_model, _rpc_vram_needed, ctx_size, request)
            else:
                _new_cluster = _form_rpc_cluster(requested_model, _rpc_vram_needed, ctx_size=ctx_size,
                                                 transport=transport, runtime=runtime_override,
                                                 **({"agent_ids": selected_ids} if selected_ids is not None else {}))
                created_here = True
            if not _new_cluster:
                avail = sorted(
                    f"{m.hostname}/{m.gpu_vram_gb}GB"
                    for m in machines.values()
                    if m.alive and (m.gpu_vram_gb or 0) > 0 and not m.in_rpc
                )
                raise HTTPException(
                    503,
                    f"Model '{requested_model}' needs ~{_rpc_vram_needed} GB VRAM combined. "
                    f"Idle GPU capacity: {avail or 'none'}.",
                    # No job or model startup was accepted. Browser clients may
                    # wait for reconnecting/free GPUs and retry this rejection.
                    headers={"X-Swarm-Status": "waiting_for_capacity", "Retry-After": "5"},
                )
            if created_here:
                created_cluster = _new_cluster
            cluster = _new_cluster
            if created_here and _new_cluster.relay_url and not _new_cluster.relay_ready:
                try:
                    if relay_provisioner and _new_cluster.relay_control_key:
                        await relay_provisioner.renew(_new_cluster.cluster_id)
                    await asyncio.to_thread(_regional_relay_call, _new_cluster, "PUT", {
                        "primary_id": _new_cluster.primary_id, "worker_ids": _new_cluster.worker_ids})
                except asyncio.CancelledError:
                    _dissolve_cluster(_new_cluster, "relay_setup_cancelled")
                    raise
                except Exception:
                    _dissolve_cluster(_new_cluster, "relay_setup_failed")
                    raise HTTPException(503, "Could not prepare the relay")
                if (_new_cluster.status not in ("forming", "active")
                        or rpc_clusters.get(_new_cluster.cluster_id) is not _new_cluster):
                    raise HTTPException(503, "RPC cluster changed during relay setup; retry")
                _new_cluster.relay_ready = True
        # Cluster forming/active — job may queue until primary is ready
    else:
        # A bounded catalog may be pulled after this request creates its queue.
        # Unknown models and models that cannot fit still fail before queuing.
        any_registered = any(m.model == requested_model for m in machines.values())
        if not any_registered and requested_model not in _ON_DEMAND_MODELS:
            raise HTTPException(503, f"No agent has ever registered model '{requested_model}'. "
                                     f"Available: {sorted({m.model for m in machines.values() if m.alive})}")
        if requested_model in _ON_DEMAND_MODELS and not _on_demand_capacity(requested_model):
            required_vram = _ON_DEMAND_MODELS[requested_model] + 1.0
            available = sorted(
                f"{m.hostname}/{m.gpu_vram_gb}GB"
                for m in machines.values() if m.alive and not m.in_rpc
            )
            raise HTTPException(
                503,
                f"Model '{requested_model}' needs ~{required_vram:.1f} GB VRAM including headroom. "
                f"Eligible workers: {available or 'none'}.",
                headers={"X-Swarm-Status": "waiting_for_capacity", "Retry-After": "5"},
            )

    # Capture stream intent before forcing non-streaming for the agent.
    # Agent always processes non-streaming and posts the full result back.
    consumer_wants_stream = body.get("stream", False)
    body["model"]  = requested_model
    body["stream"] = False

    # Output caps have already been normalized into options.num_predict.
    body.pop("max_tokens", None)

    if not _rpc_vram_needed:
        context = body["options"]["num_ctx"]
        if type(context) is not int or not 512 <= context <= 65536:
            raise HTTPException(422, "num_ctx must be an integer between 512 and 65536")
        capable = [m for m in machines.values() if m.alive and not m.in_rpc and _can_serve_single(m, requested_model)]
        if not capable:
            raise HTTPException(503, "No live worker has capacity for the requested model")
        context_limit = max(_single_context_limit(m, requested_model) for m in capable)
        if explicit_context and context > context_limit:
            raise HTTPException(503, f"Requested context {context} exceeds the eligible workers' {context_limit}-token policy")
        if not explicit_context:
            body["options"]["num_ctx"] = min(context, context_limit)

    # Mistral recommends temperature=0.15 for devstral (deterministic code generation).
    # Only apply when the client hasn't set a temperature explicitly.
    if body.get("temperature") is None and "devstral" in requested_model:
        body["temperature"] = 0.15
    # Mirror top-level temperature into options so Ollama's /api/chat (native) picks it up.
    for parameter in ("temperature", "top_p", "top_k", "min_p"):
        if body.get(parameter) is not None:
            body.setdefault("options", {}).setdefault(parameter, body[parameter])

    # Build a clean Ollama-native /api/chat body, dropping all OpenAI-only fields
    # (tool_choice, response_format, frequency_penalty, presence_penalty, n, logprobs,
    # top_logprobs, user, etc.) that Ollama's native parser rejects.
    ollama_body: dict = {
        "model":    requested_model,
        "messages": body.get("messages", []),
        "stream":   False,
        "options":  body.get("options", {}),
    }
    if not _rpc_vram_needed:
        # Ollama expects think beside options, not inside it. Preserve explicit
        # opt-ins (including the legacy nested field); default Qwen to no thinking
        # so a bounded output budget can reach the actual answer.
        think = ollama_body["options"].pop("think", False if "qwen3" in requested_model else None)
        think = body.get("think", think)
        if think is not None:
            ollama_body["think"] = think
    if body.get("tools"):
        ollama_body["tools"] = body["tools"]
    if body.get("keep_alive") is not None:
        ollama_body["keep_alive"] = body["keep_alive"]
    if body.get("format"):
        ollama_body["format"] = body["format"]

    # Regional control setup awaits I/O: recheck before the atomic enqueue.
    job = Job(body=ollama_body, model=requested_model)
    if _rpc_vram_needed:
        job.rpc_cluster_id = (created_cluster or cluster).cluster_id
        job.rpc_timeout = JOB_TIMEOUT * (2 if (created_cluster or cluster).transport == "relay" else 1)
        job.rpc_agent_ids = selected_ids
        primary = machines.get((created_cluster or cluster).primary_id)
        if consumer_wants_stream and primary and (primary.runtime or {}).get("rpc_stream"):
            job.live_stream = StreamBuffer()
            job.body["stream"] = True
            job.stream = True
    try:
        _check_chat_queue_capacity(requested_model, job.input_bytes)
    except HTTPException:
        if created_cluster and not any(
                pending.rpc_cluster_id == created_cluster.cluster_id and not pending.done_event.is_set() for pending in jobs.values()):
            _dissolve_cluster(created_cluster, "admission_rejected")
        raise
    jobs[job.id] = job
    if job.model not in job_queues:
        job_queues[job.model] = asyncio.Queue()
    await job_queues[job.model].put(job)

    # Trace admission without putting student prompts or tool content in logs.
    print(f"[job {job.id[:8]}] queued | {requested_model}")

    t_start = time.time()
    if job.live_stream is not None:
        return StreamingResponse(_stream_chat_response(job, request, user_id, t_start,
            (body.get("stream_options") or {}).get("include_usage") is True),
            media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
    if not await _await_chat_completion(job, request, user_id, t_start):
        return Response(status_code=499)

    result = _finalize_chat_response(job, user_id, t_start)
    machine = machines.get(job.assigned_agent_id)

    if consumer_wants_stream:
        # Convert non-streaming response to OpenAI-compatible SSE delta format.
        # Clients (aider, Goose) expect chunk objects with delta fields, not message.
        base_id       = result.get("id", f"chatcmpl-{uuid.uuid4().hex[:8]}")
        created       = result.get("created", int(time.time()))
        model_name    = result.get("model") or (machine.model if machine else requested_model)
        choices       = result.get("choices", [])
        msg           = choices[0].get("message", {}) if choices else {}
        content       = msg.get("content", "") or ""
        tool_calls    = msg.get("tool_calls")
        reasoning     = msg.get("reasoning_content")
        include_usage = (body.get("stream_options") or {}).get("include_usage") is True
        finish_reason = choices[0].get("finish_reason", "stop") if choices else "stop"

        async def _sse():
            # Role chunk
            role_chunk = {"id": base_id, "object": "chat.completion.chunk",
                          "created": created, "model": model_name,
                          "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
            yield f"data: {json.dumps(role_chunk)}\n\n"
            if reasoning:
                reasoning_chunk = {"id": base_id, "object": "chat.completion.chunk",
                                   "created": created, "model": model_name,
                                   "choices": [{"index": 0, "delta": {"reasoning_content": reasoning}, "finish_reason": None}]}
                yield f"data: {json.dumps(reasoning_chunk)}\n\n"
            # Content chunk
            if content:
                content_chunk = {"id": base_id, "object": "chat.completion.chunk",
                                 "created": created, "model": model_name,
                                 "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}
                yield f"data: {json.dumps(content_chunk)}\n\n"
            # Tool-calls chunk — send all tool calls in one delta
            if tool_calls:
                tc_chunk = {"id": base_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name,
                            "choices": [{"index": 0, "delta": {"tool_calls": [dict(tc, index=i) for i, tc in enumerate(tool_calls)]}, "finish_reason": None}]}
                yield f"data: {json.dumps(tc_chunk)}\n\n"
            # Final chunk
            final_chunk = {"id": base_id, "object": "chat.completion.chunk",
                           "created": created, "model": model_name,
                           "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
            yield f"data: {json.dumps(final_chunk)}\n\n"
            if include_usage and result.get("usage"):
                usage_chunk = {"id": base_id, "object": "chat.completion.chunk",
                               "created": created, "model": model_name,
                               "choices": [], "usage": result["usage"]}
                yield f"data: {json.dumps(usage_chunk)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_sse(), media_type="text/event-stream")

    return JSONResponse(content=result)

# ── media consumer endpoints ─────────────────────────────────────────────────

@app.get("/v1/images/models")
async def list_image_models(_key: str = Depends(require_consumer)):
    return {"object": "list", "data": IMAGE_MODELS}


@app.post("/v1/images/generations")
async def generate_image(request: Request, payload: ImageGenerationRequest, _key: str = Depends(require_consumer)):
    _consumer_rl.check(_real_ip(request))

    job = ImageJob(
        model           = payload.model,
        prompt          = payload.prompt,
        negative_prompt = payload.negative_prompt,
        n               = max(1, min(payload.n, 4)),
        size            = payload.size,
        quality         = payload.quality,
    )
    image_jobs[job.id] = job
    _save_image_job(job)
    if job.model not in image_job_queues:
        image_job_queues[job.model] = asyncio.Queue()
    await image_job_queues[job.model].put(job)
    print(f"[img {job.id[:8]}] {payload.model} | {payload.prompt[:80]!r}")

    # Block until the agent delivers the result — OpenAI-compatible synchronous response.
    t_start = time.time()
    while True:
        try:
            await asyncio.wait_for(job.done_event.wait(), timeout=3.0)
            break
        except asyncio.TimeoutError:
            if time.time() - t_start >= IMAGE_JOB_TIMEOUT:
                job.status = "failed"
                job.error  = "timeout"
                raise HTTPException(503, "Image generation timed out — is a media agent running?")
            if await request.is_disconnected():
                job.cancelled = True
                return Response(status_code=499)

    if job.status == "failed":
        raise HTTPException(502, f"Image generation failed: {job.error or 'unknown error'}")

    data: list[dict] = []
    for fp in job.file_paths:
        if payload.response_format == "b64_json":
            with open(fp, "rb") as fh:
                data.append({"b64_json": base64.b64encode(fh.read()).decode()})
        else:
            data.append({"url": f"/peerfoundry/api/v1/images/files/{os.path.basename(fp)}"})

    elapsed_s = round(time.time() - t_start, 1)
    print(f"[img {job.id[:8]}] done {elapsed_s}s — {len(data)} image(s)")
    return JSONResponse({"created": int(job.created_at), "data": data})


@app.get("/v1/images/generations/{job_id}")
async def get_image_job_status(job_id: str, _key: str = Depends(require_consumer)):
    job = image_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Image job not found (may have expired)")
    resp: dict = {"id": job.id, "status": job.status, "created": int(job.created_at), "model": job.model}
    if job.status == "processing" and job.progress:
        resp["progress"] = job.progress
    if job.status == "completed":
        resp["data"] = [{"url": f"/peerfoundry/api/v1/images/files/{os.path.basename(fp)}"} for fp in job.file_paths]
    if job.status == "failed" and job.error:
        resp["error"] = job.error
    return JSONResponse(resp)


@app.get("/v1/images/files/{filename}")
async def serve_image_file(filename: str, _key: str = Depends(require_consumer)):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    path = os.path.join(IMAGE_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "File not found")
    ext = os.path.splitext(filename)[1].lower()
    mt  = "image/png" if ext == ".png" else "image/jpeg" if ext in (".jpg", ".jpeg") else "application/octet-stream"
    return FileResponse(path, media_type=mt)


@app.get("/v1/video/models")
@app.get("/v1/videos/models")
async def list_video_models(_key: str = Depends(require_consumer)):
    return {"object": "list", "data": VIDEO_MODELS}


_VIDEO_CAPS = {m["id"]: m for m in VIDEO_MODELS}

@app.post("/v1/video/generations")
@app.post("/v1/videos/generations")
async def generate_video(request: Request, payload: VideoGenerationRequest, _key: str = Depends(require_consumer)):
    _consumer_rl.check(_real_ip(request))

    caps = _VIDEO_CAPS.get(payload.model, {})
    dur_min = caps.get("duration_min", 1)
    dur_max = caps.get("duration_max", 30)
    duration = max(dur_min, min(payload.duration, dur_max))

    # For fixed-resolution models, use the canonical size
    sizes = caps.get("sizes", [])
    if len(sizes) == 1:
        w, h = map(int, sizes[0].split("x"))
    else:
        w, h = payload.width, payload.height

    # Fill negative prompt from model default if caller left it empty
    if caps.get("supports_neg", True):
        neg = payload.negative_prompt or caps.get("default_neg", "")
    else:
        neg = ""

    job = VideoJob(
        model                = payload.model,
        prompt               = payload.prompt,
        negative_prompt      = neg,
        duration             = duration,
        width                = w,
        height               = h,
        num_inference_steps  = payload.num_inference_steps,
    )
    video_jobs[job.id] = job
    _save_video_job(job)
    if job.model not in video_job_queues:
        video_job_queues[job.model] = asyncio.Queue()
    await video_job_queues[job.model].put(job)
    print(f"[vid {job.id[:8]}] {payload.model} | {payload.prompt[:80]!r}")

    return JSONResponse({
        "id":      job.id,
        "object":  "video.generation",
        "status":  "queued",
        "model":   job.model,
        "created": int(job.created_at),
    }, status_code=202)


@app.get("/v1/video/generations/{job_id}")
@app.get("/v1/videos/generations/{job_id}")
async def get_video_job_status(job_id: str, _key: str = Depends(require_consumer)):
    job = video_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Video job not found (may have expired)")
    resp: dict = {
        "id":      job.id,
        "object":  "video.generation",
        "status":  job.status,
        "model":   job.model,
        "created": int(job.created_at),
    }
    if job.status == "processing" and job.progress:
        resp["progress"] = job.progress
    if job.status == "completed" and job.file_path:
        resp["url"] = f"/peerfoundry/api/v1/video/files/{os.path.basename(job.file_path)}"
    if job.status == "failed" and job.error:
        resp["error"] = job.error
    return JSONResponse(resp)


@app.get("/v1/video/files/{filename}")
@app.get("/v1/videos/files/{filename}")
async def serve_video_file(filename: str, _key: str = Depends(require_consumer)):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    path = os.path.join(VIDEO_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="video/mp4")


# ── media agent endpoints ──────────────────────────────────────────────────────

_IMAGE_VRAM = {m["id"]: m["vram_gb"] for m in IMAGE_MODELS}
_VIDEO_VRAM = {m["id"]: m["vram_gb"] for m in VIDEO_MODELS}

@app.get("/agent/media/jobs/next")
async def get_next_media_job(
    agent_id: Optional[str] = None,
    vram_gb:  Optional[float] = None,
    _key: str = Depends(require_agent),
):
    """Return next image or video job whose VRAM requirement fits the agent."""
    machine = machines.get(agent_id)
    if not machine or not machine.alive:
        raise HTTPException(409, "A registered agent_id is required to claim work")
    if not _worker_available(machine):
        return Response(status_code=204)
    # Never trust a poller's optimistic VRAM query over registered hardware.
    vram_gb = min(vram_gb, machine.gpu_vram_gb or 0) if vram_gb is not None else machine.gpu_vram_gb or 0
    for model_id, q in image_job_queues.items():
        req = _IMAGE_VRAM.get(model_id, 8)
        if vram_gb is not None and req > vram_gb:
            continue
        while not q.empty():
            try:
                job = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if job.cancelled or job.status in ("failed", "completed"):
                continue
            job.status      = "processing"
            job.dequeued_at = time.time()
            if agent_id and agent_id in machines:
                machines[agent_id].busy             = True
                machines[agent_id].busy_job_id      = job.id
                machines[agent_id].media_phase      = "RUNNING"
                machines[agent_id].media_model      = job.model
                machines[agent_id].media_updated_at = time.time()
            return {
                "id":        job.id,
                "type":      "image",
                "model":     job.model,
                "body_json": json.dumps({
                    "prompt":          job.prompt,
                    "negative_prompt": job.negative_prompt,
                    "n":               job.n,
                    "size":            job.size,
                    "quality":         job.quality,
                }),
            }

    for model_id, q in video_job_queues.items():
        req = _VIDEO_VRAM.get(model_id, 8)
        if vram_gb is not None and req > vram_gb:
            continue
        while not q.empty():
            try:
                job = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if job.status in ("failed", "completed", "cancelled") or getattr(job, "cancelled", False):
                continue  # skip stale entries left in Queue after force-fail/cancel
            job.status      = "processing"
            job.dequeued_at = time.time()
            if agent_id and agent_id in machines:
                machines[agent_id].busy             = True
                machines[agent_id].busy_job_id      = job.id
                machines[agent_id].media_phase      = "RUNNING"
                machines[agent_id].media_model      = job.model
                machines[agent_id].media_updated_at = time.time()
            vcaps = _VIDEO_CAPS.get(job.model, {})
            steps = (job.num_inference_steps
                     if getattr(job, "num_inference_steps", 0)
                     else vcaps.get("num_inference_steps", 20))
            return {
                "id":        job.id,
                "type":      "video",
                "model":     job.model,
                "body_json": json.dumps({
                    "prompt":               job.prompt,
                    "negative_prompt":      job.negative_prompt,
                    "duration":             job.duration,
                    "width":                job.width,
                    "height":               job.height,
                    "guidance_scale":       vcaps.get("guidance_scale", 5.0),
                    "num_inference_steps":  steps,
                    "fps":                  vcaps.get("fps", 8),
                }),
            }

    return Response(status_code=204)


@app.post("/agent/media/jobs/{job_id}/result")
async def submit_media_job_result(
    job_id:     str,
    request:    Request,
    job_type:   str = "image",
    ext:        str = "png",
    elapsed_ms: Optional[float] = None,
    _key: str = Depends(require_agent),
):
    existing_job = image_jobs.get(job_id) if job_type == "image" else video_jobs.get(job_id)
    if not existing_job:
        _release_media_slot(job_id)
        raise HTTPException(404, "Media job not found")
    if existing_job and existing_job.status in ("failed", "completed"):
        _release_media_slot(job_id)
        return {"status": "ok", "stale": True}
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty body")

    if job_type == "image":
        job = image_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Image job not found")
        os.makedirs(IMAGE_DIR, exist_ok=True)
        fname = f"{job_id}.{ext}"
        fpath = os.path.join(IMAGE_DIR, fname)
        with open(fpath, "wb") as f:
            f.write(data)
        job.file_paths = [fpath]
        job.elapsed_ms = elapsed_ms
        job.status     = "completed"
        job.done_event.set()
        _save_image_job(job)
        print(f"[img {job_id[:8]}] saved {fname} ({len(data)//1024} KB)")
    else:
        job = video_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Video job not found")
        os.makedirs(VIDEO_DIR, exist_ok=True)
        fname = f"{job_id}.{ext}"
        fpath = os.path.join(VIDEO_DIR, fname)
        with open(fpath, "wb") as f:
            f.write(data)
        job.file_path  = fpath
        job.elapsed_ms = elapsed_ms
        job.status     = "completed"
        _save_video_job(job)
        print(f"[vid {job_id[:8]}] saved {fname} ({len(data)//1024} KB)")

    for m in machines.values():
        if m.busy_job_id == job_id:
            m.busy             = False
            m.busy_job_id      = None
            m.media_phase      = "IDLE"
            m.media_model      = None
            m.media_updated_at = time.time()

    return {"status": "ok"}


@app.post("/agent/media/status")
async def agent_media_status(
    request: Request,
    agent_id: Optional[str] = None,
    phase:    str = "IDLE",
    model:    Optional[str] = None,
    _key: str = Depends(require_agent),
):
    """Agents call this to report their media loop phase (IDLE, DOWNLOADING_DEPS, RUNNING)."""
    key = agent_id
    if key and key in machines:
        machines[key].media_phase      = phase
        machines[key].media_model      = model
        machines[key].media_updated_at = time.time()
        if phase == "IDLE":
            m = machines[key]
            job = image_jobs.get(m.busy_job_id) or video_jobs.get(m.busy_job_id)
            if job:
                if job.status == "processing":
                    _fail_media_job(job, "Worker became idle without delivering a result")
                _release_media_slot(job.id)
    return {"status": "ok"}


@app.post("/agent/media/jobs/{job_id}/error")
async def submit_media_job_error(
    job_id:   str,
    request:  Request,
    job_type: str = "image",
    _key: str = Depends(require_agent),
):
    existing_job = image_jobs.get(job_id) if job_type == "image" else video_jobs.get(job_id)
    if not existing_job:
        _release_media_slot(job_id)
        raise HTTPException(404, "Media job not found")
    if existing_job and existing_job.status in ("failed", "completed"):
        _release_media_slot(job_id)
        return {"status": "ok", "stale": True}
    body      = await request.json()
    error_msg = body.get("error", "unknown error")

    if job_type == "image":
        job = image_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Image job not found")
        job.error  = error_msg
        job.status = "failed"
        job.done_event.set()
        _save_image_job(job)
        print(f"[img {job_id[:8]}] ERROR: {error_msg[:200]}")
    else:
        job = video_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Video job not found")
        job.error  = error_msg
        job.status = "failed"
        _save_video_job(job)
        print(f"[vid {job_id[:8]}] ERROR: {error_msg[:200]}")

    _release_media_slot(job_id)

    return {"status": "ok"}


@app.post("/agent/media/jobs/{job_id}/progress")
async def update_media_job_progress(
    job_id:   str,
    request:  Request,
    job_type: str = "video",
    _key: str = Depends(require_agent),
):
    body = await request.json()
    job  = video_jobs.get(job_id) if job_type == "video" else image_jobs.get(job_id)
    if not job:
        return {"status": "ok", "abort": True}
    if job.status in ("failed", "completed"):
        return {"status": "ok", "abort": True}
    if job.status == "queued":
        # coordinator restarted while agent was mid-job — re-claim it
        job.status = "processing"
        job.dequeued_at = time.time()
    if "downloaded_mb" in body:
        job.progress = {"phase": "downloading", "downloaded_mb": body["downloaded_mb"],
                        "total_mb": body["total_mb"], "pct": body["pct"]}
    else:
        job.progress = {"phase": "generating", "step": body["step"],
                        "total": body["total"], "pct": body["pct"]}
    return {"status": "ok", "abort": False}


# ── media job list (admin) ────────────────────────────────────────────────────

@app.get("/v1/media/jobs")
async def list_media_jobs(limit: int = 100, _key: str = Depends(require_admin)):
    """Return recent image and video jobs for the admin dashboard."""
    result = []
    for job in image_jobs.values():
        result.append({
            "id":         job.id,
            "type":       "image",
            "model":      job.model,
            "status":     job.status,
            "prompt":     job.prompt,
            "created":    int(job.created_at),
            "elapsed_ms": job.elapsed_ms,
            "error":      job.error,
            "progress":   job.progress,
            "url":        f"/peerfoundry/api/v1/images/files/{os.path.basename(job.file_paths[0])}" if job.file_paths else None,
        })
    for job in video_jobs.values():
        result.append({
            "id":         job.id,
            "type":       "video",
            "model":      job.model,
            "status":     job.status,
            "prompt":     job.prompt,
            "created":    int(job.created_at),
            "elapsed_ms": job.elapsed_ms,
            "error":      job.error,
            "progress":   job.progress,
            "url":        f"/peerfoundry/api/v1/video/files/{os.path.basename(job.file_path)}" if job.file_path else None,
        })
    result.sort(key=lambda x: x["created"], reverse=True)
    return {"jobs": result[:limit]}


# ── entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
