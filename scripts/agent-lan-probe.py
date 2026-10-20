#!/usr/bin/env python3
"""Run a bounded, bidirectional LAN connectivity test on two installed agents."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def summarize_results(rows, transport):
    """UDP evidence must never advertise a usable TCP relay host."""
    passed = sum(bool(row.get("passed")) for row in rows)
    return {"status": "passed" if passed == 2 else "partial" if passed else "blocked",
            "relay_candidate_ids": [r["peer_id"] for r in rows if r.get("passed")] if transport == "tcp" else [],
            "direct_udp_verified": transport == "udp" and passed == 2}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Publish a temporary probe manifest for the selected agents")
    parser.add_argument("--hosts", nargs=2)
    parser.add_argument("--transport", choices=("tcp", "udp"), default="tcp")
    parser.add_argument("--wait-seconds", type=int, default=1200, help="Wait for safe automatic agent updates; never stop model jobs")
    args = parser.parse_args()
    values = dict(os.environ)
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            values.setdefault(name.strip(), value.strip().strip("\"'"))
    def state():
        request = urllib.request.Request("http://127.0.0.1:8081/swarm/api/machines",
                                         headers={"X-API-Key": values["ADMIN_KEY"]})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    def selected(snapshot):
        return [m for m in snapshot["machines"] if m.get("alive") and not m.get("pending")
                and (m["hostname"] in args.hosts if args.hosts else (m.get("gpu_vram_gb") or 0) > 0)]
    nodes = selected(state())
    if len(nodes) != 2 or len({m.get("lan_ip") for m in nodes}) != 2 or any(not m.get("lan_ip") for m in nodes):
        raise RuntimeError("Select exactly two live agents with distinct registered LAN addresses")
    ids = {m["agent_id"] for m in nodes}
    print(json.dumps({"agents": [{k: m[k] for k in ("hostname", "agent_id", "lan_ip")} for m in nodes]}), flush=True)
    if not args.apply:
        print("Preview only; --apply waits for the current revision and runs the three-minute probe.")
        return 0
    os.umask(0o077)
    dest = ROOT / ".rpc-trial" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-lan-probe")
    dest.mkdir(parents=True)
    run_id = str(uuid.uuid4())
    manifest = ROOT / "agents/lan-probe.json"
    if manifest.exists() and json.loads(manifest.read_text()).get("expires_at", 0) > time.time():
        raise RuntimeError("A connectivity probe is already scheduled")
    result = {"run_id": run_id, "status": "waiting", "transport": args.transport, "agents": [{k: m[k] for k in ("hostname", "agent_id", "lan_ip")} for m in nodes], "observations": []}
    def save():
        (dest / "result.json").write_text(json.dumps(result, indent=2))
    save()
    def publish(plan):
        fd, name = tempfile.mkstemp(dir=manifest.parent, prefix=".lan-probe-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(plan, stream)
            os.chmod(name, 0o644)  # short-lived non-secret trigger, served by existing agent download route
            os.replace(name, manifest)
        finally:
            if os.path.exists(name): os.unlink(name)
    try:
        expected = hashlib.sha256((ROOT / "agents/agent.py").read_text().replace("\r\n", "\n").encode()).hexdigest()
        until = time.monotonic() + args.wait_seconds
        while True:
            nodes = [m for m in state()["machines"] if m.get("agent_id") in ids]
            ready = len(nodes) == 2 and all(m.get("alive") and not m.get("busy") and not m.get("in_rpc")
                and (m.get("runtime") or {}).get("agent_revision") == expected for m in nodes)
            print(json.dumps({"waiting_for_safe_update": not ready, "agents": [{"hostname": m["hostname"],
                "revision": (m.get("runtime") or {}).get("agent_revision"), "busy": m.get("busy"), "in_rpc": m.get("in_rpc")} for m in nodes]}), flush=True)
            if ready: break
            if time.monotonic() >= until:
                raise RuntimeError("Probe pending: agents have not both safely updated and become idle")
            time.sleep(15)
        now = time.time()
        plan = {"run_id": run_id, "token": secrets.token_hex(16), "transport": args.transport, "created_at": now, "expires_at": now + 180,
                "agents": [{"agent_id": m["agent_id"], "ip": m["lan_ip"]} for m in nodes], "endpoints": {}}
        result["started_at"] = datetime.now(timezone.utc).isoformat(); publish(plan); save()
        completed = {}
        while time.time() < plan["expires_at"]:
            for node in state()["machines"]:
                if node.get("agent_id") not in ids or not node.get("detail", "").startswith("LAN_PROBE "):
                    continue
                row = json.loads(node["detail"][len("LAN_PROBE "):])
                if (row.get("run_id") != run_id or row.get("agent_id") != node["agent_id"]
                        or row.get("transport", "tcp") != args.transport):
                    continue
                result["observations"].append(row)
                if row.get("port"):
                    plan["endpoints"][node["agent_id"]] = {"ip": row["ip"], "port": row["port"]}
                if row.get("stage") == "complete":
                    completed[node["agent_id"]] = row
            publish(plan); save()
            if len(completed) == 2:
                result["results"] = list(completed.values())
                result.update(summarize_results(result["results"], args.transport))
                print(json.dumps({"status": result["status"], "results": result["results"],
                                  "relay_candidate_ids": result["relay_candidate_ids"],
                                  "direct_udp_verified": result["direct_udp_verified"]}), flush=True)
                break
            time.sleep(1)
        else:
            result["status"] = "incomplete"
            result["results"] = list(completed.values())
    except Exception as exc:
        result.update(status="failed", error=str(exc))
        print(json.dumps({"error": str(exc)}), flush=True)
    finally:
        if manifest.exists() and json.loads(manifest.read_text()).get("run_id") == run_id:
            manifest.unlink()
        result["finished_at"] = datetime.now(timezone.utc).isoformat(); save()
    print(json.dumps({"status": result["status"], "evidence": str(dest / "result.json")}), flush=True)
    return 0 if result["status"] in ("passed", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())
