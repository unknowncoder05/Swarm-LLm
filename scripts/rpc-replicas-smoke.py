#!/usr/bin/env python3
"""Verify two active 30B replicas serve ordinary consumer requests concurrently."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
MODEL = "qwen3:30b-a3b"


def main():
    values = dict(os.environ)
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip().strip("\"'"))
    def api(path, payload=None, consumer=False, user=None):
        headers = {"X-API-Key": values["CONSUMER_KEY" if consumer else "ADMIN_KEY"], "Content-Type": "application/json"}
        if user:
            headers["X-User-ID"] = user
        request = urllib.request.Request("http://127.0.0.1:8081/peerfoundry/api" + path,
            data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        with urllib.request.urlopen(request, timeout=300 if payload else 10) as response:
            return json.load(response)
    before = api("/machines")
    clusters = [c for c in before.get("rpc_clusters", []) if c["model"] == MODEL and c["status"] == "active"]
    if len(clusters) != 2 or any(c["ctx_size"] != 32768 or c["transport"] != "quic" for c in clusters):
        raise RuntimeError("Exactly two active 32K QUIC replicas are required; this script does not create clusters")
    primaries = {c["primary_id"] for c in clusters}
    expected_hosts = {m["hostname"] for m in before["machines"] if m["agent_id"] in primaries}
    if before.get("queued") or any(m.get("busy") for m in before["machines"] if m["agent_id"] in primaries):
        raise RuntimeError("Wait until both primaries are idle before this isolated concurrency check")
    run_id = "replicas-" + uuid.uuid4().hex
    users = [run_id + "-alpha", run_id + "-beta"]
    markers = ["REPLICA_ALPHA_READY", "REPLICA_BETA_READY"]
    barrier = threading.Barrier(3)
    evidence = {"run_id": run_id, "status": "running", "clusters_before": clusters,
                "requests": [], "snapshots": []}
    def send(index):
        body = {"model": MODEL, "stream": False, "options": {"num_ctx": 32768},
                "messages": [{"role": "user", "content": "Reply with exactly " + markers[index] + "."}]}
        barrier.wait()
        started = time.monotonic()
        response = api("/v1/chat/completions", body, consumer=True, user=users[index])
        return {"user_id": users[index], "expected": markers[index], "request": body, "response": response,
                "elapsed_seconds": round(time.monotonic() - started, 3)}
    os.umask(0o077)
    path = ROOT / ".rpc-trial" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-two-30b-replicas.json")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(send, i) for i in range(2)]
            barrier.wait()
            while not all(future.done() for future in futures):
                state = api("/machines")
                evidence["snapshots"].append({"time": time.time(), "primaries": [
                    {"hostname": m["hostname"], "busy": m["busy"], "rpc_cluster_id": m["rpc_cluster_id"]}
                    for m in state["machines"] if m["agent_id"] in primaries]})
                time.sleep(.5)
            evidence["requests"] = [future.result() for future in futures]
        query = ("import os,sqlite3,json,sys; c=sqlite3.connect(os.environ['DB_PATH']); c.row_factory=sqlite3.Row; "
                 "print(json.dumps([dict(r)for r in c.execute('SELECT user_id,hostname,success,elapsed_ms,completion_tokens FROM usage_logs WHERE user_id IN (?,?)',sys.argv[1:])]))")
        result = subprocess.run(["docker", "compose", "exec", "-T", "coordinator", "python", "-c", query, *users],
                                cwd=ROOT, capture_output=True, text=True, timeout=15, check=True)
        rows = json.loads(result.stdout)
        evidence["usage_rows"] = rows
        after = api("/machines")
        evidence["clusters_after"] = after.get("rpc_clusters", [])
        evidence["native_processes_after"] = [{"hostname": m["hostname"], "agent_id": m["agent_id"],
            "rpc_pid": (m.get("runtime") or {}).get("rpc_pid"), "gpu_used_mb": (m.get("runtime") or {}).get("gpu_used_mb")}
            for m in after["machines"] if m.get("in_rpc")]
        overlap = any(len(s["primaries"]) == 2 and all(p["busy"] for p in s["primaries"]) for s in evidence["snapshots"])
        answers = all(r["response"]["choices"][0]["message"]["content"].strip() == r["expected"]
                      for r in evidence["requests"])
        if not (answers and overlap and len(rows) == 2 and all(r["success"] for r in rows)
                and {r["hostname"] for r in rows} == expected_hosts):
            raise RuntimeError("Responses, overlap and per-request serving host did not prove two independent replicas")
        evidence["status"] = "passed"
    except Exception as exc:
        evidence.update(status="failed", error=type(exc).__name__ + ": " + str(exc)[:500])
    finally:
        evidence["finished_at"] = time.time()
        path.write_text(json.dumps(evidence, indent=2))
    print(json.dumps({"status": evidence["status"], "error": evidence.get("error"), "usage_rows": evidence.get("usage_rows"),
                      "evidence": str(path)}), flush=True)
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
