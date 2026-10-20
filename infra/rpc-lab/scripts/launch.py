#!/usr/bin/env python3
"""Run the uploaded application snapshot with explicit, lab-only adaptations."""
import json
import os
from pathlib import Path
import subprocess
import sys


def configure_agent(agent, config):
    # Avoid release auto-download, wrong model-store discovery, and code drift.
    bins = {
        "llama_server": "/usr/local/bin/llama-server",
        "llama_rpc_server": "/usr/local/bin/llama-rpc-server",
    }
    agent.ensure_llamacpp = lambda: bins
    agent.find_ollama = lambda: "/opt/swarm/ollama/bin/ollama"
    agent._ollama_models_dir = lambda: Path(os.environ["OLLAMA_MODELS"])
    agent._check_for_update = lambda *args, **kwargs: None
    agent.start_media_loop = lambda *args, **kwargs: None
    # The ordinary worker assumes at least 6 GB even on a tiny GPU. Prevent it
    # from pulling the test model's queue before the RPC polling thread pauses it.
    original_model_vram = agent.model_vram
    agent.model_vram = lambda model: float("inf") if model == config["model"] else original_model_vram(model)
    original_primary = agent.run_rpc_primary

    def primary(assignment, coordinator, api_key):
        assignment = dict(assignment, ctx_size=config["context_size"])
        return original_primary(assignment, coordinator, api_key)

    agent.run_rpc_primary = primary
    original_wait = agent._wait_llama_ready
    # First load at 1 Gbps can outlast the application's five-minute load timeout.
    agent._wait_llama_ready = lambda port, timeout=120: original_wait(port, max(timeout, 900))


def main():
    config = json.loads(Path("/opt/swarm/config.json").read_text())
    value = subprocess.check_output([
        "aws", "ssm", "get-parameter", "--region", config["region"],
        "--name", config["key_parameter"], "--with-decryption",
        "--query", "Parameter.Value", "--output", "text",
    ], text=True).strip()
    if config["role"] == "coordinator":
        os.environ.update(json.loads(value))
        os.environ.update(DB_PATH="/data/coordinator.db",
                          CONTAINER_ARTIFACT_DIR="/data/container-artifacts")
        import coordinator
        import uvicorn
        # Raise only the lab model's estimate for explicit 3/4-node trials, so
        # all requested nodes join. No reported hardware capacity is fabricated.
        coordinator._RPC_MODELS[config["model"]] = max(
            config["required_vram_gb"], (config["node_count"] - 1) * config["node_vram_gb"] + 0.1)
        uvicorn.run(coordinator.app, host="0.0.0.0", port=8080)
    else:
        import agent
        configure_agent(agent, config)
        # The large GGUF is already downloaded. No small model is downloaded unless
        # a separate client requests it; ordinary Ollama handling remains present.
        sys.argv = ["agent.py", "--coordinator", config["coordinator"],
                    "--api-key", value, "--model", "qwen3:8b", "--skip-model-pull"]
        agent.main()


if __name__ == "__main__":
    main()
