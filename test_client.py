#!/usr/bin/env python3
"""
peerfoundry test client
─────────────────────
Polls until at least one machine is registered, then fires a coding prompt
and streams the response. Verifies the full pipeline end-to-end.

Usage:
    python3 test_client.py

Reads API_KEY from .env in the same directory.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import ssl
from scripts.gen_opencode_config import load_base_url

# ── config ────────────────────────────────────────────────────────────────────

COORDINATOR = load_base_url().rsplit("/v1", 1)[0]
ENV_FILE    = os.path.join(os.path.dirname(__file__), ".env")
TIMEOUT     = 600   # seconds to wait for a machine to register
TEST_PROMPT = (
    "Write a Python function that takes a list of integers and returns "
    "a new list with duplicates removed, preserving the original order. "
    "Add a brief docstring."
)

# ── load API key ──────────────────────────────────────────────────────────────

def _read_env_key(name: str) -> str:
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip()
    return ""

admin_key    = _read_env_key("ADMIN_KEY")
consumer_key = _read_env_key("CONSUMER_KEY")

if not admin_key or not consumer_key:
    print("[!] ADMIN_KEY and CONSUMER_KEY must be set in .env")
    sys.exit(1)

ctx = ssl.create_default_context()
# If domain cert isn't resolving yet, uncomment to skip verification:
# ctx.check_hostname = False
# ctx.verify_mode = ssl.CERT_NONE

def api_get(path: str, key: str) -> dict:
    req = urllib.request.Request(
        f"{COORDINATOR}{path}",
        headers={"X-API-Key": key},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        return json.loads(r.read())

def api_post_stream(path: str, body: dict, key: str):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{COORDINATOR}{path}",
        data=data,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, context=ctx, timeout=120)

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n  PeerFoundry test client")
    print("  ─────────────────────")

    # 1. Verify coordinator is up
    print("\n[1] Checking coordinator... ", end="", flush=True)
    try:
        stats = api_get("/machines", admin_key)
        print(f"UP — {stats['total']} registered, {stats['alive']} alive")
    except Exception as e:
        print(f"FAILED: {e}")
        sys.exit(1)

    # 2. Wait for at least one live machine
    print(f"\n[2] Waiting for an agent to register (timeout {TIMEOUT}s)...")
    deadline = time.time() + TIMEOUT
    machine_info = None
    while time.time() < deadline:
        try:
            data = api_get("/machines", admin_key)
            live = [m for m in data["machines"] if m["alive"]]
            if live:
                machine_info = live[0]
                break
        except Exception:
            pass
        dots = "." * (int(time.time()) % 4)
        print(f"\r    waiting{dots:<4}  ({int(deadline - time.time())}s left)  ", end="", flush=True)
        time.sleep(2)

    if not machine_info:
        print("\n\n[!] No live machines registered within timeout.")
        print("    Run agent.ps1 on a Windows machine and point it to this coordinator.")
        sys.exit(1)

    print(f"\n    Machine connected!")
    print(f"    Host   : {machine_info['hostname']} ({machine_info['key']})")
    print(f"    Model  : {machine_info['model']}")
    cpu = machine_info.get('cpu_name', '?')
    gpu = machine_info.get('gpu_name') or "CPU only"
    ram = f"{machine_info.get('ram_free_gb', '?')} / {machine_info.get('ram_total_gb', '?')} GB"
    print(f"    CPU    : {cpu}")
    print(f"    GPU    : {gpu}")
    print(f"    RAM    : {ram}")

    # 3. Send coding prompt (streaming)
    print(f"\n[3] Sending test prompt...")
    print(f"    \"{TEST_PROMPT[:80]}...\"")
    print("\n" + "─" * 60)

    payload = {
        "model":  machine_info["model"],
        "stream": True,
        "messages": [
            {"role": "user", "content": TEST_PROMPT}
        ],
    }

    t_start = time.time()
    token_count = 0
    full_response = ""

    try:
        with api_post_stream("/v1/chat/completions", payload, consumer_key) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if delta:
                        print(delta, end="", flush=True)
                        full_response += delta
                        token_count += 1
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception as e:
        print(f"\n[!] Streaming error: {e}")
        sys.exit(1)

    elapsed = time.time() - t_start
    print("\n" + "─" * 60)
    print(f"\n[4] Done.")
    print(f"    Time     : {elapsed:.1f}s")
    print(f"    Tokens   : ~{token_count} (delta chunks)")
    print(f"    Tok/s    : ~{token_count / elapsed:.1f}")
    print(f"    Machine  : {machine_info['hostname']} ({machine_info['model']})")
    print("\n  Pipeline verified. Ready for production use.\n")

if __name__ == "__main__":
    main()
