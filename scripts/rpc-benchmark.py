#!/usr/bin/env python3
"""Compare bounded 30B RPC runtime profiles on an explicitly selected idle pair."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODEL = "qwen3:30b-a3b"
PROFILES = {
    "baseline": {"batch_size": 128, "ubatch_size": 64, "cache_ram": 8192},
    "cache-off": {"batch_size": 128, "ubatch_size": 64, "cache_ram": 0},
    "batch256": {"batch_size": 256, "ubatch_size": 128, "cache_ram": 8192},
    "batch512": {"batch_size": 512, "ubatch_size": 128, "cache_ram": 8192},
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", nargs=2, required=True)
    parser.add_argument("--profiles", nargs="+", choices=PROFILES, default=list(PROFILES))
    parser.add_argument("--base-url", default="http://127.0.0.1:8081/peerfoundry/api")
    parser.add_argument("--wait-seconds", type=int, default=1200)
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=2)
    parser.add_argument("--apply", action="store_true", help="Run trials, restarting only the selected pair when idle")
    args = parser.parse_args()
    values = dict(os.environ)
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1); values.setdefault(k.strip(), v.strip().strip("\"'"))
    if not values.get("ADMIN_KEY"):
        parser.error("ADMIN_KEY is required")

    def api(path, payload=None, timeout=20):
        request = urllib.request.Request(args.base_url.rstrip("/") + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"X-API-Key": values["ADMIN_KEY"], "Content-Type": "application/json",
                     "X-User-ID": "rpc-performance-trial"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def members(state):
        nodes = [m for m in state["machines"] if m.get("alive") and m.get("hostname") in args.hosts]
        if len(nodes) != 2 or {m["hostname"] for m in nodes} != set(args.hosts):
            raise RuntimeError("Both requested machines must be online; no substitution is allowed")
        if any(m.get("last_seen_sec", 999) > 30 for m in nodes):
            raise RuntimeError("Selected agents must have fresh heartbeats")
        return nodes

    state = api("/machines"); nodes = members(state)
    ids = {m["agent_id"] for m in nodes}
    if not args.apply:
        print(json.dumps({"hosts": args.hosts, "profiles": {p: PROFILES[p] for p in args.profiles},
                          "context": 32768, "model": MODEL, "repeats": args.repeats}))
        return 0
    expected = hashlib.sha256((ROOT / "agents/agent.py").read_bytes()).hexdigest()
    if any((m.get("runtime") or {}).get("agent_revision") != expected or
           not (m.get("runtime") or {}).get("rpc_tuning") for m in nodes):
        raise RuntimeError("Selected agents must be safely updated before tuning")
    os.umask(0o077)
    dest = ROOT / ".rpc-trial" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-performance")
    dest.mkdir(parents=True)
    result = {"started_at": time.time(), "hosts": args.hosts, "ids": sorted(ids), "model": MODEL,
              "context": 32768, "agent_revision": expected, "profiles": {}, "status": "running"}
    path = dest / "result.json"
    def save():
        path.write_text(json.dumps(result, indent=2))
    save(); print(json.dumps({"evidence": str(path)}), flush=True)

    def wait_idle_and_stop():
        until = time.monotonic() + args.wait_seconds
        while True:
            state = api("/machines"); nodes = members(state)
            if {m["agent_id"] for m in nodes} != ids:
                raise RuntimeError("Selected agent identities changed")
            clusters = [c for c in state.get("rpc_clusters", []) if ids & {c["primary_id"], *c["worker_ids"]}]
            if not clusters and all(not m.get("in_rpc") and not m.get("busy") and
                                    not (m.get("runtime") or {}).get("rpc_pid") for m in nodes):
                return
            for cluster in clusters:
                if {cluster["primary_id"], *cluster["worker_ids"]} != ids or cluster["model"] != MODEL:
                    raise RuntimeError("Selected machines are assigned to another workload")
                if cluster["status"] == "active":
                    try:
                        api("/rpc/clusters/" + cluster["cluster_id"] + "/stop", {})
                    except urllib.error.HTTPError as exc:
                        if exc.code != 409:
                            raise
            if time.monotonic() > until:
                raise RuntimeError("Selected machines did not become safely idle")
            time.sleep(3)

    short = [{"role": "user", "content": "What is 7 multiplied by 8? Reply with only the number."}]
    reference = "\n".join("def item_%04d(x): return x + %d" % (n, n % 17) for n in range(450))
    long = [{"role": "user", "content": "Reference Python code:\n" + reference +
             "\nWhat is 7 multiplied by 8? Reply with only the number."}]
    followup = [*long, {"role": "assistant", "content": "56"},
                {"role": "user", "content": "Confirm the same multiplication. Reply with only the number."}]
    try:
        for name in args.profiles:
            wait_idle_and_stop()
            profile = PROFILES[name]
            entry = {"settings": profile, "samples": []}; result["profiles"][name] = entry
            cases = [("startup", short)]
            for repeat in range(args.repeats):
                cases.extend([("long-%s" % repeat, long), ("followup-%s" % repeat, followup),
                              ("short-%s" % repeat, short), ("restore-%s" % repeat, long)])
            for label, messages in cases:
                started = time.monotonic()
                response = api("/v1/chat/completions", {"model": MODEL, "rpc_transport": "quic",
                    "rpc_agent_ids": sorted(ids), "rpc_runtime": profile, "stream": False,
                    "messages": messages, "temperature": 0, "max_tokens": 512,
                    "options": {"num_ctx": 32768}}, timeout=1900)
                choice = response["choices"][0]
                sample = {"case": label, "seconds": round(time.monotonic() - started, 3),
                    "answer": choice["message"].get("content"), "finish_reason": choice.get("finish_reason"),
                    "timings": response.get("timings", {}), "usage": response.get("usage", {})}
                sample["passed"] = str(sample["answer"]).strip() == "56" and sample["finish_reason"] == "stop"
                entry["samples"].append(sample)
                save(); print(json.dumps({"profile": name, **sample}), flush=True)
                if not sample["passed"]:
                    raise RuntimeError("Trial answer failed; no performance profile will be recommended")
            active = api("/machines")
            entry["cluster"] = next(c for c in active["rpc_clusters"] if {c["primary_id"], *c["worker_ids"]} == ids)
            if entry["cluster"]["ctx_size"] != 32768 or entry["cluster"]["runtime_profile"] != profile:
                raise RuntimeError("Actual cluster settings did not match the trial")
            primary = next(m for m in active["machines"] if m["agent_id"] == entry["cluster"]["primary_id"])
            runtime = primary.get("runtime") or {}
            entry["gpu_layers"] = runtime.get("rpc_gpu_layers")
            entry["total_layers"] = runtime.get("rpc_total_layers")
            entry["gpu_allocations_mb"] = {m["hostname"]: (m.get("runtime") or {}).get("gpu_used_mb")
                for m in active["machines"] if m.get("agent_id") in ids}
            if not entry["total_layers"] or entry["gpu_layers"] != entry["total_layers"]:
                raise RuntimeError("Cannot confirm that the full model remains on GPUs; rejecting this profile")
            entry["median_seconds"] = statistics.median(s["seconds"] for s in entry["samples"] if s["case"] != "startup")
            save()
        result["status"] = "passed"
    except Exception as exc:
        result["status"] = "failed"; result["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        result["finished_at"] = time.time(); save()
    print(json.dumps({"status": result["status"], "medians": {k: v.get("median_seconds") for k, v in result["profiles"].items()},
                      "evidence": str(path)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
