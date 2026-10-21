"""In-memory machine and job entities; no application globals or I/O."""

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import statistics
import time
from typing import Optional
import uuid

try:
    from api_models import RegisterPayload
except ModuleNotFoundError:
    from coordinator.api_models import RegisterPayload


class MachineState:
    def __init__(self, ip: str, reg: RegisterPayload):
        self.ip = ip
        self.port = reg.port
        self.agent_id = reg.agent_id or f"{ip}:{reg.port}"
        self.model = reg.model
        self.hostname = reg.hostname or ip
        self.cpu_name = reg.cpu_name or "unknown"
        self.cpu_cores = reg.cpu_cores
        self.ram_total_gb = reg.ram_total_gb
        self.ram_free_gb = reg.ram_free_gb
        self.gpu_name = reg.gpu_name
        self.gpu_vram_gb = reg.gpu_vram_gb
        self.network_mbps = reg.network_mbps
        self.ollama_version = reg.ollama_version or "?"
        self.os_version = reg.os_version or "unknown"
        self.runtime: Optional[dict] = None
        self.phase = "RUNNING"
        self.detail = ""
        self.registered_at = time.time()
        self.last_heartbeat = time.time()
        self.alive = True
        self.busy = False
        self.busy_job_id: Optional[str] = None
        self.busy_claim_token: Optional[str] = None
        self.busy_claim_attempt = 0
        self.busy_claim_request_id: Optional[str] = None
        self.busy_chat_model: Optional[str] = None
        # Delayed active heartbeats must not resurrect acknowledged work.
        # This covers recent reordering, not coordinator restart recovery.
        self.completed_job_ids: deque[str] = deque(maxlen=64)
        self.in_rpc = False
        self.rpc_cluster_id: Optional[str] = None
        self.lan_ip: Optional[str] = reg.lan_ip
        self.media_phase: Optional[str] = (
            None  # IDLE | DOWNLOADING_DEPS | RUNNING | None=unknown
        )
        self.media_model: Optional[str] = None
        self.media_updated_at: Optional[float] = None
        self.requests_ok = 0
        self.requests_err = 0
        self.response_times: deque[float] = deque(maxlen=20)

    @property
    def key(self) -> str:
        return self.agent_id

    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{self.port}"

    @property
    def last_seen_ago(self) -> float:
        return time.time() - self.last_heartbeat

    @property
    def avg_response_ms(self) -> Optional[float]:
        return statistics.mean(self.response_times) if self.response_times else None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "ip": self.ip,
            "port": self.port,
            "hostname": self.hostname,
            "model": self.model,
            "alive": self.alive,
            "last_seen_sec": round(self.last_seen_ago, 1),
            "registered_at": datetime.fromtimestamp(
                self.registered_at, tz=timezone.utc
            ).isoformat(),
            "cpu_name": self.cpu_name,
            "cpu_cores": self.cpu_cores,
            "ram_total_gb": self.ram_total_gb,
            "ram_free_gb": self.ram_free_gb,
            "gpu_name": self.gpu_name,
            "gpu_vram_gb": self.gpu_vram_gb,
            "network_mbps": self.network_mbps,
            "ollama_version": self.ollama_version,
            "os_version": self.os_version,
            "runtime": self.runtime,
            "phase": self.phase,
            "detail": self.detail,
            "requests_ok": self.requests_ok,
            "requests_err": self.requests_err,
            "avg_response_ms": round(self.avg_response_ms, 1)
            if self.avg_response_ms
            else None,
            "busy": self.busy,
            "in_rpc": self.in_rpc,
            "rpc_cluster_id": self.rpc_cluster_id,
            "lan_ip": self.lan_ip,
            "media_phase": self.media_phase,
            "media_model": self.media_model,
            "media_updated_sec": round(time.time() - self.media_updated_at, 0)
            if self.media_updated_at
            else None,
        }


class PendingAgent:
    def __init__(self, ip: str, hostname: Optional[str], model: Optional[str]):
        self.ip = ip
        self.hostname = hostname or ip
        self.model = model or "?"
        self.phase = "CONNECTING"
        self.detail = ""
        self.updated_at = time.time()

    def to_dict(self) -> dict:
        return {
            "key": f"{self.ip}:?",
            "ip": self.ip,
            "port": None,
            "hostname": self.hostname,
            "model": self.model,
            "alive": False,
            "pending": True,
            "phase": self.phase,
            "detail": self.detail,
            "updated_at": datetime.fromtimestamp(
                self.updated_at, tz=timezone.utc
            ).isoformat(),
            # nulls so the UI doesn't break on missing fields
            "last_seen_sec": round(time.time() - self.updated_at, 1),
            "registered_at": None,
            "cpu_name": None,
            "cpu_cores": None,
            "ram_total_gb": None,
            "ram_free_gb": None,
            "gpu_name": None,
            "gpu_vram_gb": None,
            "network_mbps": None,
            "ollama_version": None,
            "os_version": None,
            "requests_ok": 0,
            "requests_err": 0,
            "avg_response_ms": None,
        }


class Job:
    def __init__(self, body: dict, model: str):
        self.id = str(uuid.uuid4())
        self.body = body
        self.input_bytes = _chat_payload_bytes(body)
        self.model = model
        self.stream = body.get("stream", False)
        self.created_at = time.time()
        self.done_event = asyncio.Event()
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.upstream_status: Optional[int] = None
        self.elapsed_ms: Optional[float] = None
        self.cancelled: bool = False
        self.assigned_agent_id: Optional[str] = None
        self.claim_token: Optional[str] = None
        self.attempts = 0
        self.rpc_cluster_id: Optional[str] = None
        self.rpc_recovering = False
        self.rpc_agent_ids = None
        self.rpc_recoveries = 0
        self.rpc_timeout: Optional[float] = None
        self.live_stream = None
        self.stream_started = False


class ImageJob:
    def __init__(
        self,
        model: str,
        prompt: str,
        negative_prompt: str,
        n: int,
        size: str,
        quality: str,
    ):
        self.id = str(uuid.uuid4())
        self.model = model
        self.prompt = prompt
        self.negative_prompt = negative_prompt
        self.n = n
        self.size = size
        self.quality = quality
        self.created_at = time.time()
        self.status = "queued"  # queued | processing | completed | failed
        self.dequeued_at: Optional[float] = None
        self.done_event = asyncio.Event()
        self.file_paths: list[str] = []
        self.error: Optional[str] = None
        self.elapsed_ms: Optional[float] = None
        self.cancelled: bool = False
        self.progress: Optional[dict] = None  # {"step": 5, "total": 50, "pct": 10}


class VideoJob:
    def __init__(
        self,
        model: str,
        prompt: str,
        negative_prompt: str,
        duration: int,
        width: int,
        height: int,
        num_inference_steps: int = 0,
    ):
        self.id = str(uuid.uuid4())
        self.model = model
        self.prompt = prompt
        self.negative_prompt = negative_prompt
        self.duration = duration
        self.width = width
        self.height = height
        self.num_inference_steps = num_inference_steps  # 0 = use catalog default
        self.created_at = time.time()
        self.status = "queued"  # queued | processing | completed | failed
        self.dequeued_at: Optional[float] = None
        self.file_path: Optional[str] = None
        self.error: Optional[str] = None
        self.elapsed_ms: Optional[float] = None
        self.progress: Optional[dict] = None  # {"step": 5, "total": 50, "pct": 10}


def _chat_payload_bytes(body: dict) -> int:
    # Match body_json's wire representation, including escaped Unicode.
    return len(json.dumps(body).encode("utf-8"))
