#!/usr/bin/env python3
"""Verify the public consumer API, including waiting for a cold RPC cluster."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    values = dict(os.environ)
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            values.setdefault(name.strip(), value.strip().strip("\"'"))
    key = values.get("CONSUMER_KEY")
    if not key:
        raise SystemExit("CONSUMER_KEY is required")
    url = "https://sandbox.yerson.co/swarm/api/v1/chat/completions"
    result = {"model": "qwen3:30b-a3b", "url": url,
              "started_at": datetime.now(timezone.utc).isoformat()}
    payload = {"model": result["model"], "stream": False,
               "messages": [{"role": "user", "content": "Reply with exactly ready. /no_think"}],
               "temperature": 0, "options": {"num_ctx": 2048}}
    print("Submitting public Qwen3 30B consumer request; may wait for model loading", flush=True)
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, data=json.dumps(payload).encode(),
            headers={"X-API-Key": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=3700) as response:
            result["response"] = json.load(response)
        content = result["response"]["choices"][0]["message"]["content"].strip()
        result["status"] = "passed" if content == "ready" else "failed"
        print(json.dumps({"status": result["status"], "content": content}), flush=True)
    except Exception as exc:
        result["status"], result["error"] = "failed", str(exc)
        if isinstance(exc, urllib.error.HTTPError):
            result["error_body"] = exc.read().decode(errors="replace")
        print(json.dumps({"status": result["status"], "error": str(exc)}), flush=True)
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    os.umask(0o077)
    directory = ROOT / ".rpc-trial"
    directory.mkdir(exist_ok=True)
    destination = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-public-rpc.json")
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Elapsed: {result['elapsed_seconds']}s; evidence: {destination}", flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
