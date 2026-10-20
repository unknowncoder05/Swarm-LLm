#!/usr/bin/env python3
"""Inspect or exercise a two-machine RPC cluster through the coordinator API."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODEL = "qwen3:30b-a3b"


def select_trial_machines(state, hosts=None, allow_extra=False):
    candidates = [m for m in state["machines"] if m.get("alive") and
                  (m.get("gpu_vram_gb") or 0) > 0 and not m.get("in_rpc") and not m.get("busy")
                  and m.get("media_phase") != "RUNNING"]
    # Legacy routes do not pin hosts. QUIC trials can explicitly reserve the
    # selected pair, leaving any other newly joined GPUs available.
    if hosts:
        if allow_extra:
            candidates = [m for m in candidates if m.get("hostname") in hosts]
        if set(hosts) != {m.get("hostname") for m in candidates}:
            return []
    return sorted(candidates, key=lambda m: m.get("hostname") or "")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="Read status without queuing inference")
    parser.add_argument("--watch", action="store_true", help="Watch compact RPC progress every 30 seconds without queuing work")
    parser.add_argument("--base-url", default="http://127.0.0.1:8081/swarm/api")
    parser.add_argument("--wait-seconds", type=int, default=360)
    parser.add_argument("--transport", choices=("direct", "relay", "quic", "auto"), default="direct")
    parser.add_argument("--context", type=int, default=2048)
    parser.add_argument("--hosts", nargs=2, metavar="MACHINE", help="Select two machines; defaults to exactly two idle live GPUs")
    args = parser.parse_args()
    values = dict(os.environ)
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values.setdefault(key.strip(), value.strip().strip("\"'"))
    key = values.get("ADMIN_KEY")
    if not key:
        parser.error("ADMIN_KEY must be supplied in the environment or project .env")

    def api(path, payload=None, timeout=10):
        request = urllib.request.Request(args.base_url.rstrip("/") + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"X-API-Key": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    source = (ROOT / "agent/agent.py").read_text(encoding="utf-8")
    revisions = {hashlib.sha256(source.encode()).hexdigest(),
                 hashlib.sha256(source.replace("\n", "\r\n").encode()).hexdigest()}

    def inspect():
        state = api("/machines")
        print(json.dumps({"alive": state["alive"], "pending": state["pending"], "queued": state.get("queued", 0),
            "clusters": state.get("rpc_clusters", []), "machines": [
                {**{field: m.get(field) for field in ("hostname", "alive", "last_seen_sec", "lan_ip", "phase", "detail", "runtime", "in_rpc", "busy")},
                 "updated": (m.get("runtime") or {}).get("agent_revision") in revisions}
                for m in state["machines"] if m.get("alive") or m.get("pending") or
                (args.hosts and m.get("hostname") in args.hosts)]}), flush=True)
        return state

    if args.watch:
        while True:
            state = api("/machines")
            print(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "clusters": [
                {field: c.get(field) for field in ("model", "status", "transport", "relay_bytes", "primary_ready")}
                for c in state.get("rpc_clusters", [])], "machines": [
                {"hostname": m.get("hostname"), "alive": m.get("alive"),
                 "rpc_pid": (m.get("runtime") or {}).get("rpc_pid"),
                 "gpu_used_mb": (m.get("runtime") or {}).get("gpu_used_mb")}
                for m in state["machines"] if m.get("alive") or m.get("in_rpc")]}), flush=True)
            time.sleep(30)

    if args.status:
        inspect()
        reports = api("/agent-reports?limit=5")["reports"]
        for report in reports:
            print(json.dumps({"hostname": report["hostname"], "received_at": report["received_at"],
                "model": report.get("model"), "detail": report["detail"][:1024]}), flush=True)
        return 0

    evidence = {"model": MODEL, "transport": args.transport, "source_revisions": sorted(revisions), "started_at": time.time(),
                "status": "starting"}
    os.umask(0o077)
    result_dir = ROOT / ".rpc-trial"
    result_dir.mkdir(exist_ok=True)
    result_path = result_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
    try:
        deadline = time.monotonic() + args.wait_seconds
        while True:
            state = inspect()
            live = select_trial_machines(state, args.hosts, allow_extra=args.transport in ("quic", "auto"))
            ready = len(live) == 2 and all((args.transport in ("relay", "auto") or m.get("lan_ip")) and
                (m.get("runtime") or {}).get("agent_revision") in revisions and
                (m.get("runtime") or {}).get("ollama_ready") is True for m in live)
            if ready:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Exactly two idle GPU agents must be online with current code and healthy Ollama; direct transport also needs private LAN addresses")
            time.sleep(15)
        evidence["machines_before"] = live
        for label, prompt in [("cold", "Reply with exactly ready. /no_think"),
                              ("warm", "What is 2 + 2? Reply with only the answer. /no_think")]:
            print("Submitting " + label + " distributed prompt", flush=True)
            started = time.monotonic()
            response = api("/v1/chat/completions", {"model": MODEL, "stream": False,
                "rpc_transport": args.transport,
                **({"rpc_agent_ids": [m["agent_id"] for m in live]} if args.transport in ("quic", "auto") else {}),
                "messages": [{"role": "user", "content": prompt}], "temperature": 0,
                "options": {"num_ctx": args.context}}, timeout=3700 if args.transport == "relay" else 1900)
            evidence[label] = {"elapsed_seconds": round(time.monotonic()-started, 3), "response": response}
            print(json.dumps(evidence[label]), flush=True)
            reply = response["choices"][0]["message"]["content"].strip().lower()
            if (label == "cold" and "ready" not in reply) or (label == "warm" and "4" not in reply):
                raise RuntimeError("Unexpected smoke-test response")
        state = api("/machines")
        selected_ids = {m["agent_id"] for m in live}
        cluster = next((c for c in state.get("rpc_clusters", []) if c["model"] == MODEL
                        and {c["primary_id"], *c["worker_ids"]} == selected_ids), None)
        if not cluster or cluster["status"] != "active" or not cluster["primary_ready"] or len(cluster["worker_ids"]) != 1:
            raise RuntimeError("Inference returned without an active two-machine RPC cluster")
        if args.transport != "auto" and cluster["transport"] != args.transport:
            raise RuntimeError("Inference used a different transport than the requested trial")
        if args.transport == "auto" and (not cluster.get("automatic") or
                set(cluster.get("preflight", {}).values()) != {"verified", "native_ready"}):
            raise RuntimeError("Automatic inference requires verified route checks before model startup")
        if cluster["transport"] == "quic":
            stats = cluster.get("quic_stats", {})
            if len(stats) != 2 or not all(row.get("sent_bytes", 0) > 0 and row.get("received_bytes", 0) > 0
                                          for row in stats.values()):
                raise RuntimeError("Both agents must report direct QUIC payload transfer")
        if {cluster["primary_id"], *cluster["worker_ids"]} != {m["agent_id"] for m in live}:
            raise RuntimeError("The scheduler used different agents than the selected trial pair")
        evidence["cluster"] = cluster
        for attempt in range(4):
            members = [m for m in state["machines"] if m.get("rpc_cluster_id") == cluster["cluster_id"]]
            if len(members) == 2 and all((m.get("runtime") or {}).get("rpc_pid") and
                    ((m.get("runtime") or {}).get("gpu_used_mb") or 0) >= 4096 for m in members):
                break
            if attempt == 3:
                raise RuntimeError("Both RPC processes must report at least 4 GiB of GPU allocation")
            time.sleep(10)
            state = api("/machines")
        evidence["machines_after"] = members
        evidence["status"] = "passed"
    except Exception as exc:
        evidence["status"] = "failed"
        evidence["error"] = str(exc)
        if isinstance(exc, urllib.error.HTTPError):
            evidence["error_body"] = exc.read().decode("utf-8", errors="replace")
        print(json.dumps({k: evidence[k] for k in ("status", "error", "error_body") if k in evidence}), flush=True)
    finally:
        evidence["finished_at"] = time.time()
        result_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print("Evidence: " + str(result_path), flush=True)
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
