#!/usr/bin/env python3
"""Prepare private coordinator settings and a local systemd provisioner unit."""
import argparse
import os
from pathlib import Path
import secrets
import tempfile
import ipaddress

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, help="Host's private Docker gateway address")
    parser.add_argument("--apply", action="store_true", help="Write private settings and the reviewable service unit")
    args = parser.parse_args()
    if not ipaddress.ip_address(args.bind).is_private or args.bind in ("0.0.0.0", "127.0.0.1"):
        parser.error("Use the host-only Docker gateway, not a public or wildcard address")
    unit = f"""[Unit]
Description=Terraform lifecycle for the on-demand Swarm relay
After=network-online.target docker.service
Wants=network-online.target
[Service]
User={os.environ.get('USER', 'ubuntu')}
WorkingDirectory={ROOT}
ExecStart=/usr/bin/python3 {ROOT}/scripts/relay-provisioner.py --bind {args.bind}
Environment=PATH=/snap/bin:/usr/local/bin:/usr/bin:/bin
Environment=TERRAFORM_BINARY=/snap/terraform/current/terraform
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={ROOT}/infra/relay {ROOT}/.rpc-trial/relay-provisioner
PrivateTmp=true
[Install]
WantedBy=multi-user.target
"""
    if not args.apply:
        print(unit)
        print("Preview only; --apply writes settings and the unit, without restarting services.")
        return
    path = ROOT / ".env"
    lines = path.read_text().splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.lstrip().startswith("#"))
    key = values.get("RPC_RELAY_PROVISIONER_KEY") or secrets.token_urlsafe(40)
    settings = {"RPC_RELAY_PROVISIONER_URL": "http://host.docker.internal:8092",
                "RPC_RELAY_PROVISIONER_KEY": key, "RPC_DEFAULT_TRANSPORT": "auto",
                "RPC_CLOUD_RELAY_ENABLED": "true"}
    content = "\n".join([line for line in lines if line.split("=", 1)[0].strip() not in settings]
                        + [f"{k}={v}" for k, v in settings.items()]) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".env-provisioner-", dir=ROOT)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
    os.replace(temporary, path)
    state = ROOT / ".rpc-trial/relay-provisioner"
    state.mkdir(parents=True, exist_ok=True)
    unit_path = state / "swarm-relay-provisioner.service"
    unit_path.write_text(unit)
    print(f"Private settings written; install and start the unit at {unit_path}")


if __name__ == "__main__":
    main()
