"""Typed agent, container and media payloads for the HTTP API."""

import re
from typing import Any, Optional
from fastapi import HTTPException
from pydantic import BaseModel, Field


class RegisterPayload(BaseModel):
    port: int
    model: str
    agent_id: Optional[str] = None  # stable UUID generated on agent; used as key
    agent_pid: Optional[int] = Field(default=None, gt=0)
    hostname: Optional[str] = None
    cpu_name: Optional[str] = None
    cpu_cores: Optional[int] = None
    ram_total_gb: Optional[float] = None
    ram_free_gb: Optional[float] = None
    gpu_name: Optional[str] = None
    gpu_vram_gb: Optional[float] = None
    network_mbps: Optional[float] = None
    ollama_version: Optional[str] = None
    os_version: Optional[str] = None
    lan_ip: Optional[str] = None  # agent's LAN IP for RPC peer connectivity


class RpcLogPayload(BaseModel):
    role: str
    modified_at: float
    tail: str = Field(max_length=4096)
    process_running: bool


class HeartbeatPayload(BaseModel):
    port: int
    agent_id: Optional[str] = None  # same UUID sent at registration
    agent_pid: Optional[int] = Field(default=None, gt=0)
    supervised: Optional[bool] = None
    ollama_models: Optional[list[dict[str, Any]]] = Field(default=None, max_length=8)
    rpc_log: Optional[RpcLogPayload] = None
    agent_revision: Optional[str] = Field(default=None, max_length=64)
    ollama_pid: Optional[int] = Field(default=None, gt=0)
    ollama_ready: Optional[bool] = None
    rpc_pid: Optional[int] = Field(default=None, gt=0)
    gpu_used_mb: Optional[float] = Field(default=None, ge=0)
    rpc_relay_external: bool = False
    rpc_quic: bool = False
    rpc_preflight: bool = False
    rpc_tuning: bool = False
    rpc_stream: bool = False
    rpc_gpu_layers: Optional[int] = Field(default=None, ge=0, le=10000)
    rpc_total_layers: Optional[int] = Field(default=None, ge=0, le=10000)
    active_job_id: Optional[str] = Field(default=None, max_length=128)
    active_claim_token: Optional[str] = Field(default=None, max_length=128)


class ContainerJobCreate(BaseModel):
    image: str
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    timeout_sec: int = 3600
    cpus: float = 1.0
    memory_mb: int = 1024
    network: bool = False
    platform: Optional[str] = None


class ContainerJobResult(BaseModel):
    exit_code: int
    elapsed_ms: float
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None


class AgentStatusPayload(BaseModel):
    phase: str  # CONNECTING | INSTALLING | DOWNLOADING | STARTING | REGISTERING
    detail: str = ""
    agent_id: Optional[str] = None
    hostname: Optional[str] = None
    model: Optional[str] = None
    report_id: Optional[str] = Field(default=None, max_length=128)
    traceback: str = Field(default="", max_length=16384)
    log_tail: str = Field(default="", max_length=16384)
    exit_code: Optional[int] = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    occurred_at: Optional[float] = None


class RpcReadyPayload(BaseModel):
    agent_id: str
    cluster_id: str
    role: str  # "primary" | "worker"


class RpcPreflightPayload(RpcReadyPayload):
    stage: str


class JobResultPayload(BaseModel):
    result: Optional[dict] = None
    error: Optional[str] = None
    elapsed_ms: Optional[float] = None
    claim_token: Optional[str] = None
    upstream_status: Optional[int] = Field(default=None, ge=400, le=599, strict=True)


class ImageGenerationRequest(BaseModel):
    model: str = "flux-schnell"
    prompt: str
    negative_prompt: str = ""
    n: int = 1
    size: str = "1024x1024"
    quality: str = "standard"
    response_format: str = "url"


class VideoGenerationRequest(BaseModel):
    model: str = "ltx-video"
    prompt: str
    negative_prompt: str = ""
    duration: int = 5
    width: int = 512
    height: int = 512
    num_inference_steps: int = 0  # 0 = use catalog default


def _validate_container_job(spec: ContainerJobCreate) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}", spec.image):
        raise HTTPException(
            422, "image must be a Docker/OCI image reference, not a URL"
        )
    if not (1 <= spec.timeout_sec <= 86400):
        raise HTTPException(422, "timeout_sec must be between 1 and 86400")
    if not (0.1 <= spec.cpus <= 64):
        raise HTTPException(422, "cpus must be between 0.1 and 64")
    if not (64 <= spec.memory_mb <= 262144):
        raise HTTPException(422, "memory_mb must be between 64 and 262144")
    if len(spec.command) > 128 or any(len(v) > 8192 for v in spec.command):
        raise HTTPException(422, "command is too large")
    if len(spec.env) > 128 or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) for k in spec.env
    ):
        raise HTTPException(
            422, "invalid environment variable name or too many variables"
        )
    for path in spec.artifacts:
        if (
            not path
            or path.startswith(("/", "\\"))
            or ".." in path.split("/")
            or len(path) > 512
        ):
            raise HTTPException(
                422, "artifact paths must be relative paths below /workspace"
            )
