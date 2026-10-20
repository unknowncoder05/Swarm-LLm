#!/usr/bin/env python3
"""Run the private, host-local Terraform relay lifecycle service."""
import argparse
import fcntl
import logging
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coordinator.relay_provisioner import RelayLifecycle, TerraformRelay, serve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--idle-seconds", type=int, default=300)
    args = parser.parse_args()
    os.umask(0o077)
    values = dict(os.environ)
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            values.setdefault(k.strip(), v.strip().strip("\"'"))
    state = ROOT / ".rpc-trial/relay-provisioner"
    state.mkdir(parents=True, exist_ok=True)
    lock = (state / "service.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    logging.basicConfig(level=logging.INFO)
    lifecycle = RelayLifecycle(TerraformRelay(ROOT), state / "leases.json", args.idle_seconds)
    serve(lifecycle, values.get("RPC_RELAY_PROVISIONER_KEY", ""), args.bind, args.port)


if __name__ == "__main__":
    main()
