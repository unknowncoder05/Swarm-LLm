#!/usr/bin/env python3
"""Exercise real coordinator admission, RPC formation, and two inference calls."""
import json
from pathlib import Path
import subprocess
import time
from urllib.request import Request, urlopen


def request(base, key, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request(base + path, data=data,
                  headers={"X-API-Key": key, "Content-Type": "application/json"})
    with urlopen(req, timeout=1800) as response:
        return json.load(response)


def verify_capacity(machines, expected, required=20.0):
    live = [m for m in machines if m["alive"]]
    if len(live) != expected:
        raise RuntimeError("Expected {} live GPU agents, got {}".format(expected, len(live)))
    memory = [m.get("gpu_vram_gb") or 0 for m in live]
    if any(v <= 0 for v in memory) or max(memory) >= required or sum(memory) < required:
        raise RuntimeError("Trial needs GPU memory below {} GB individually and enough in total: {}".format(required, memory))
    return live


def verify_cluster(state, model, expected=2):
    clusters = [c for c in state["rpc_clusters"] if c["model"] == model and c["status"] == "active"]
    if len(clusters) != 1 or not clusters[0]["worker_ids"]:
        raise RuntimeError("No active multi-machine RPC cluster after inference")
    cluster = clusters[0]
    members = {cluster["primary_id"]} | set(cluster["worker_ids"])
    if len(members) != expected:
        raise RuntimeError("Expected {} cluster members, got {}".format(expected, len(members)))
    active = {m["key"] for m in state["machines"] if m["alive"] and m.get("in_rpc")}
    if not members.issubset(active):
        raise RuntimeError("Cluster members are missing or not reserved for RPC")
    return cluster


def main():
    config = json.loads(Path("/opt/swarm/config.json").read_text())
    keys = json.loads(subprocess.check_output([
        "aws", "ssm", "get-parameter", "--region", config["region"],
        "--name", config["key_parameter"], "--with-decryption",
        "--query", "Parameter.Value", "--output", "text",
    ], text=True))
    base = "http://127.0.0.1:8080"
    admin = keys["ADMIN_KEY"]
    state = request(base, admin, "/machines")
    live = verify_capacity(state["machines"], config["node_count"], config["required_vram_gb"])
    model = config["model"]
    models = request(base, keys["CONSUMER_KEY"], "/v1/models")
    if model not in {m["id"] for m in models["data"]}:
        raise RuntimeError("Large model is not advertised by the coordinator")
    results = []
    for prompt in ("Reply with the word ready.", "What is 2 + 2? Give a short answer."):
        if model.startswith("qwen"):
            prompt += " /no_think"
        started = time.monotonic()
        result = request(base, keys["CONSUMER_KEY"], "/v1/chat/completions", {
            "model": model, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0, "stream": False,
        })
        message = result["choices"][0]["message"]
        if not message.get("content", "").strip():
            raise RuntimeError("Inference returned no answer text")
        results.append({"elapsed_seconds": round(time.monotonic() - started, 3),
                        "usage": result.get("usage"), "answer": message["content"]})
    cluster = verify_cluster(request(base, admin, "/machines"), model, config["node_count"])
    print(json.dumps({
        "status": "passed", "model": model, "cluster": cluster,
        "context_size": config["context_size"],
        "source_revision": config["revision"],
        "agents": [{"id": m["key"], "hostname": m["hostname"],
                    "gpu": m["gpu_name"], "vram_gb": m["gpu_vram_gb"]} for m in live],
        "requests": results,
        "note": "Inspect CUDA and RPC allocation logs on the primary to confirm actual GPU offload.",
    }, indent=2))


if __name__ == "__main__":
    main()
