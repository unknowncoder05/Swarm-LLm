#!/usr/bin/env python3
"""Verify real incremental 30B SSE through the public consumer endpoint."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import urllib.request

from gen_opencode_config import load_consumer_key

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    config = json.loads((ROOT / "opencode.json").read_text())
    base = args.base_url or config["provider"]["swarm-llm"]["options"]["baseURL"]
    key = load_consumer_key(ROOT)
    if not key:
        parser.error("A consumer key is required")
    body = {"model": "qwen3:30b-a3b", "stream": True, "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "What is 7 multiplied by 8? Reply with only the number."}],
        "temperature": 0, "max_tokens": 512, "options": {"num_ctx": 32768}}
    request = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
        headers={"X-API-Key": key, "Content-Type": "application/json", "X-User-ID": "rpc-stream-smoke"})
    start = time.monotonic(); chunks = []; first_delta = None; first_event = None; done = False
    os.umask(0o077)
    dest = ROOT / ".rpc-trial" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-stream")
    dest.mkdir(parents=True)
    with urllib.request.urlopen(request, timeout=args.timeout) as response:
        headers_at = time.monotonic() - start
        for line in response:
            if time.monotonic() - start > args.timeout:
                raise TimeoutError("Stream smoke exceeded its overall deadline")
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                done = True
                break
            chunk = json.loads(data)
            chunks.append(chunk)
            if first_event is None:
                first_event = time.monotonic() - start
            if any(c.get("delta", {}).get("content") or c.get("delta", {}).get("reasoning_content")
                   for c in chunk.get("choices", [])) and first_delta is None:
                first_delta = time.monotonic() - start
                print(json.dumps({"first_delta_seconds": round(first_delta, 3)}), flush=True)
    elapsed = time.monotonic() - start
    answer = "".join(c.get("delta", {}).get("content") or "" for e in chunks for c in e.get("choices", []))
    usage = [e["usage"] for e in chunks if e.get("usage")]
    finishes = [c["finish_reason"] for e in chunks for c in e.get("choices", []) if c.get("finish_reason")]
    result = {"headers_seconds": round(headers_at, 3), "first_event_seconds": first_event,
        "first_delta_seconds": first_delta, "elapsed_seconds": round(elapsed, 3), "answer": answer,
        "event_count": len(chunks), "usage": usage, "finish_reasons": finishes,
        "reasoning_present": any(c.get("delta", {}).get("reasoning_content") for e in chunks for c in e.get("choices", [])),
        "passed": done and answer.strip() == "56" and len(usage) == 1 and finishes == ["stop"]
                  and first_delta is not None and elapsed - first_delta > .5}
    (dest / "result.json").write_text(json.dumps(result, indent=2))
    (dest / "events.json").write_text(json.dumps(chunks, indent=2))
    print(json.dumps(dict(result, evidence=str(dest))), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
