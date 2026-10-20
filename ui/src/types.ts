export interface Machine {
  agent_id?: string;
  runtime?: {
    agent_pid: number;
    agent_revision: string | null;
    supervised?: boolean | null;
    ollama_models?: { name?: string; size?: number; size_vram?: number; context_length?: number }[] | null;
    ollama_pid: number | null;
    ollama_ready: boolean | null;
    rpc_pid?: number | null;
    gpu_used_mb?: number | null;
  } | null;
  last_error?: {
    detail: string;
    received_at: number;
    traceback: string;
    log_tail: string;
    exit_code: number | null;
    diagnostics: Record<string, unknown>;
  } | null;
  key: string;
  ip: string;
  port: number | null;
  hostname: string;
  model: string;
  alive: boolean;
  pending?: boolean;
  phase?: string;
  detail?: string;
  last_seen_sec: number;
  registered_at: string | null;
  cpu_name: string | null;
  cpu_cores: number | null;
  ram_total_gb: number | null;
  ram_free_gb: number | null;
  gpu_name: string | null;
  gpu_vram_gb: number | null;
  network_mbps: number | null;
  ollama_version: string | null;
  os_version: string | null;
  requests_ok: number;
  requests_err: number;
  avg_response_ms: number | null;
  busy?: boolean;
  in_rpc?: boolean;
  rpc_cluster_id?: string | null;
  media_phase?: string | null;
  media_model?: string | null;
  media_updated_sec?: number | null;
}

export interface RpcCluster {
  cluster_id: string;
  model: string;
  primary_id: string;
  worker_ids: string[];
  status: string;
  primary_ready: boolean;
  transport: string;
  route_id?: string;
  preflight?: Record<string, string>;
  ctx_size?: number;
  relay_url?: string | null;
  dissolve_reason?: string | null;
  quic_stats?: Record<string, {
    sent_bytes: number;
    received_bytes: number;
    connected: boolean;
  }>;
}

export interface MachinesResponse {
  total: number;
  alive: number;
  dead: number;
  pending: number;
  queued?: number;
  machines: Machine[];
  rpc_clusters?: RpcCluster[];
}

export interface UsageSeries {
  bucket: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  requests: number;
  errors: number;
  avg_ms: number | null;
  avg_queue_ms: number | null;
}

export interface UsageByModel {
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  requests: number;
  errors: number;
  avg_ms: number | null;
  avg_queue_ms: number | null;
}

export interface UsageByUser {
  user_id: string;
  total_tokens: number;
  requests: number;
}

export interface UsageByHour {
  hour: number;
  label: string;
  requests: number;
  avg_ms: number | null;
}

export interface UsageByFinishReason {
  reason: string;
  count: number;
}

export interface UsageByHostname {
  hostname: string;
  requests: number;
  total_tokens: number;
  avg_ms: number | null;
  avg_queue_ms: number | null;
}

export interface UsageStats {
  from_ts: number;
  to_ts: number;
  bucket: string;
  series: UsageSeries[];
  by_model: UsageByModel[];
  by_user: UsageByUser[];
  by_hour: UsageByHour[];
  by_finish_reason: UsageByFinishReason[];
  by_hostname: UsageByHostname[];
}

export interface MediaModel {
  id: string;
  name: string;
  vram_gb: number;
  hf_repo: string;
  duration_min?: number;
  duration_max?: number;
  duration_default?: number;
  sizes?: string[];          // e.g. ["512x512","704x480"] — first is default
  supports_neg?: boolean;
  fps?: number;
  note?: string;
}

export interface ImageResult {
  url: string;
}

export interface ImageGenResponse {
  created: number;
  data: ImageResult[];
}

export interface ImageGenJob {
  id: string;
  status: "queued" | "processing" | "completed" | "failed";
  created: number;
  model?: string;
  data?: ImageResult[];
  error?: string;
}

export interface JobProgress {
  step: number;
  total: number;
  pct: number;
}

export interface VideoGenResponse {
  id: string;
  status: "queued" | "processing" | "completed" | "failed";
  created: number;
  model?: string;
  url?: string;
  error?: string;
  progress?: JobProgress;
}

export interface MediaJob {
  id: string;
  type: "image" | "video";
  model: string;
  status: "queued" | "processing" | "completed" | "failed";
  prompt: string;
  created: number;
  elapsed_ms?: number | null;
  error?: string | null;
  progress?: JobProgress | null;
  url?: string | null;
}

export interface MediaJobsResponse {
  jobs: MediaJob[];
}
