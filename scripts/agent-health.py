#!/usr/bin/env python3
"""Audit live installations, API access controls, and optional per-agent inference."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
from gen_opencode_config import load_base_url

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8081/swarm/api")
    parser.add_argument("--public-url", default=load_base_url(ROOT).rsplit("/api/v1", 1)[0])
    parser.add_argument("--hosts", nargs="+", help="Audit these hostnames; otherwise all live GPU agents")
    parser.add_argument("--expected-machines", type=int, default=2)
    parser.add_argument("--wait-seconds", type=int, default=360)
    parser.add_argument("--smoke", action="store_true", help="Submit short concurrent prompts and observe which agents serve them")
    args = parser.parse_args()
    values = dict(os.environ)
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            values.setdefault(name.strip(), value.strip().strip("\"'"))
    for name in ("ADMIN_KEY", "AGENT_KEY", "CONSUMER_KEY"):
        if not values.get(name):
            parser.error(name + " is required")

    def request(url, key=None, payload=None, timeout=10):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-API-Key"] = key
        req = urllib.request.Request(url, headers=headers,
            data=json.dumps(payload).encode() if payload is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def api(path, payload=None, timeout=10):
        status, raw = request(args.base_url.rstrip("/") + path, values["ADMIN_KEY"], payload, timeout)
        if status != 200:
            raise RuntimeError(f"{path}: HTTP {status}: {raw[:300].decode(errors='replace')}")
        return json.loads(raw)

    def digest(raw):
        return hashlib.sha256(raw.decode("utf-8").replace("\r\n", "\n").encode()).hexdigest()

    expected_revision = digest((ROOT / "agents/agent.py").read_bytes())
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "revision": expected_revision, "checks": []}
    def check(name, passed, detail=None):
        row = {"check": name, "passed": bool(passed)}
        if detail is not None:
            row["detail"] = detail
        result["checks"].append(row)
        print(json.dumps(row), flush=True)

    def selected(state):
        return [m for m in state["machines"] if m.get("alive") and not m.get("pending") and
                (m.get("hostname") in args.hosts if args.hosts else m.get("alive") and (m.get("gpu_vram_gb") or 0) > 0)]

    try:
        status, raw = request(args.public_url.rstrip("/") + "/api/healthz")
        check("public_https", status == 200 and json.loads(raw).get("status") == "ok")
        status, raw = request(args.public_url.rstrip("/") + "/agent/agent.py")
        check("public_agent_revision", status == 200 and digest(raw) == expected_revision)
        status, raw = request(args.base_url.rstrip("/") + "/agent/agent.py", values["AGENT_KEY"])
        check("authenticated_agent_revision", status == 200 and digest(raw) == expected_revision)
        for path, key_name, expected in [
            ("/machines", None, 401), ("/machines", "AGENT_KEY", 401),
            ("/machines", "CONSUMER_KEY", 401), ("/agent-reports", "AGENT_KEY", 401),
            ("/agent-reports", "CONSUMER_KEY", 401), ("/v1/models", None, 401),
            ("/v1/models", "AGENT_KEY", 401), ("/v1/models", "CONSUMER_KEY", 200)]:
            status, _ = request(args.base_url.rstrip("/") + path, values.get(key_name) if key_name else None)
            check(f"auth:{path}:{key_name or 'anonymous'}", status == expected, {"status": status, "expected": expected})

        deadline = time.monotonic() + args.wait_seconds
        while True:
            state = api("/machines")
            nodes = selected(state)
            ready = len(nodes) == args.expected_machines and all(m.get("alive") and not m.get("in_rpc") and
                (m.get("runtime") or {}).get("ollama_ready") is True and
                (m.get("runtime") or {}).get("agent_revision") == expected_revision and
                ("Windows" not in (m.get("os_version") or "") or (m.get("runtime") or {}).get("supervised") is True)
                for m in nodes)
            print(json.dumps({"waiting": not ready, "machines": [{"hostname": m["hostname"],
                "alive": m["alive"], "runtime": m.get("runtime")} for m in nodes]}), flush=True)
            if ready or time.monotonic() >= deadline:
                break
            time.sleep(15)
        check("current_healthy_supervised_agents", ready, {"count": len(nodes), "expected": args.expected_machines})
        result["machines_before"] = nodes

        if args.smoke and ready:
            if any(m.get("busy") for m in nodes):
                raise RuntimeError("An agent is already busy; refusing to mix audit evidence with existing jobs")
            observed = set()
            smoke_start = time.monotonic()
            def infer(index, model):
                expected = f"SWARM_READY_{index}"
                response = api("/v1/chat/completions", {"model": model, "stream": False,
                    "messages": [{"role": "user", "content": f"Reply with exactly {expected} and nothing else."}],
                    "temperature": 0, "options": {"num_ctx": 2048}}, timeout=600)
                content = response["choices"][0]["message"]["content"]
                return {"model": model, "expected": expected, "content": content,
                        "passed": content.strip() == expected, "usage": response.get("usage")}
            with ThreadPoolExecutor(max_workers=len(nodes)) as pool:
                futures = [pool.submit(infer, i + 1, m["model"]) for i, m in enumerate(nodes)]
                while not all(future.done() for future in futures):
                    snapshot = api("/machines")
                    observed.update(m["agent_id"] for m in selected(snapshot) if m.get("busy"))
                    time.sleep(.5)
                responses = [future.result() for future in futures]
            result["smoke"] = {"elapsed_seconds": round(time.monotonic() - smoke_start, 3),
                               "observed_agent_ids": sorted(observed), "responses": responses}
            check("inference_responses", all(r["passed"] for r in responses), responses)
            check("inference_observed_on_every_agent", {m["agent_id"] for m in nodes} <= observed)
            placement = []
            for attempt in range(5):
                placement = []
                for node in selected(api("/machines")):
                    model = next((m for m in (node.get("runtime") or {}).get("ollama_models") or []
                                  if m.get("name") == node["model"]), {})
                    size, gpu = model.get("size") or 0, model.get("size_vram") or 0
                    placement.append({"hostname": node["hostname"], "model": node["model"],
                        "size": size, "size_vram": gpu,
                        "gpu_fraction": round(gpu / size, 3) if size else None,
                        "passed": size > 0 and gpu >= .8 * size})
                if len(placement) == len(nodes) and all(p["passed"] for p in placement):
                    break
                if attempt < 4:
                    time.sleep(5)  # allow the next 15-second heartbeat to arrive
            check("model_gpu_placement_at_least_80_percent", len(placement) == len(nodes) and
                  all(p["passed"] for p in placement), placement)

        result["machines_after"] = selected(api("/machines"))
        ids = {m["agent_id"] for m in nodes}
        result["reports"] = [r for r in api("/agent-reports?limit=100")["reports"] if r.get("agent_id") in ids]
        check("no_failures_for_current_installations", not result["reports"], {"reports": len(result["reports"])})
        result["status"] = "passed" if all(c["passed"] for c in result["checks"]) else "failed"
    except Exception as exc:
        result["status"], result["error"] = "failed", str(exc)
        print(json.dumps({"error": str(exc)}), flush=True)
    os.umask(0o077)
    directory = ROOT / ".rpc-trial"
    directory.mkdir(exist_ok=True)
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    destination = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-agent-health.json")
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "evidence": str(destination)}), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
