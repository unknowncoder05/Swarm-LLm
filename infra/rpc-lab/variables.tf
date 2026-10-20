variable "name" {
  type    = string
  default = "swarm-rpc-lab"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,29}$", var.name))
    error_message = "Use 3–30 lowercase letters, digits, or hyphens, starting with a letter."
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "availability_zone" {
  description = "Choose an AZ offering the selected GPU instance. Null selects the first available AZ."
  type        = string
  default     = null
}

variable "trial_size" {
  description = "small: two fractional L4 GPUs on On-Demand with Llama 3.2 3B Q8; large: 16 GiB T4 GPUs on capped Spot with Qwen3 32B. No automatic market fallback."
  type        = string
  default     = "small"
  validation {
    condition     = contains(["small", "large"], var.trial_size)
    error_message = "Choose small or large."
  }
}

variable "node_count" {
  type    = number
  default = 2
  validation {
    condition     = var.node_count >= 2 && var.node_count <= 4 && floor(var.node_count) == var.node_count
    error_message = "Use 2–4 GPU nodes. Larger trials require deliberate configuration changes."
  }
}

variable "gpu_instance_type" {
  description = "Large profile only: one 16 GiB T4 per g4dn.xlarge. The 32B model exceeds one GPU. Small always uses g6f.large."
  type        = string
  default     = "g4dn.xlarge"
  validation {
    condition     = contains(["g4dn.xlarge", "g4dn.2xlarge"], var.gpu_instance_type)
    error_message = "Use g4dn.xlarge or g4dn.2xlarge to preserve the one-16-GiB-GPU-per-machine experiment."
  }
}

variable "spot_max_price" {
  description = "Maximum USD per GPU instance-hour: Spot request cap for large; live On-Demand pricing guard for small."
  type        = number
  default     = 0.30
  validation {
    condition     = var.spot_max_price > 0 && var.spot_max_price <= 0.526
    error_message = "Set a positive GPU hourly cap no greater than 0.526 USD."
  }
}

variable "max_runtime_hours" {
  description = "Each instance shuts down and terminates after this time from boot, including bootstrap. Destroy still cleans all remaining resources."
  type        = number
  default     = 2
  validation {
    condition     = var.max_runtime_hours >= 1 && var.max_runtime_hours <= 8
    error_message = "Use a run-time limit between 1 and 8 hours."
  }
}

variable "gpu_ami_id" {
  description = "Optional compatible pinned AMI. Large expects DLAMI with NVIDIA/CUDA; small expects Ubuntu permitting GRID installation. Null uses public SSM lookup."
  type        = string
  default     = null
}

variable "agent_source_path" {
  description = "Optional path to a reviewed RPC-capable agent.py. Null uses the captured lab snapshot, since the separate agent checkout is ignored by this repository."
  type        = string
  default     = null
}

variable "llama_cpp_ref" {
  description = "Shared llama.cpp release tag or commit; never use latest or master for an experiment."
  type        = string
  default     = "v0.6.0"
  validation {
    condition     = can(regex("^(v[0-9]+\\.[0-9]+\\.[0-9]+|b[0-9]+|[a-f0-9]{40})$", var.llama_cpp_ref))
    error_message = "Use a release tag or full lowercase commit SHA."
  }
}

variable "ollama_version" {
  type    = string
  default = "v0.40.2"
  validation {
    condition     = can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+$", var.ollama_version))
    error_message = "Use an explicit Ollama release version."
  }
}

variable "context_size" {
  description = "Lab-only context override; production assigns 16384. Start smaller for a short capacity trial."
  type        = number
  default     = 1024
  validation {
    condition     = contains([1024, 4096, 8192, 16384], var.context_size)
    error_message = "Use 1024, 4096, 8192, or 16384 tokens."
  }
}

variable "peer_network_mbps" {
  description = "GPU-node private-subnet egress cap to approximate a LAN. Zero disables traffic shaping."
  type        = number
  default     = 1000
  validation {
    condition     = var.peer_network_mbps == 0 || (var.peer_network_mbps >= 100 && var.peer_network_mbps <= 10000)
    error_message = "Use zero, or a LAN rate between 100 and 10000 Mbps."
  }
}

variable "peer_latency_ms" {
  description = "Added private-network egress delay per GPU node; a round trip sees twice this delay."
  type        = number
  default     = 1
  validation {
    condition     = var.peer_latency_ms >= 0 && var.peer_latency_ms <= 20
    error_message = "Use a delay between 0 and 20 milliseconds."
  }
}
