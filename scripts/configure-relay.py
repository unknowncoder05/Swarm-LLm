#!/usr/bin/env python3
"""Configure the existing coordinator from Terraform's relay outputs."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the reviewed settings to .env; does not restart services")
    parser.add_argument("--disable", action="store_true", help="Remove relay settings before destroying the trial")
    parser.add_argument("--idle-timeout-seconds", type=int, choices=range(60, 3601), metavar="60..3600",
                        help="Also set the native model idle lifetime; longer values reserve GPUs for longer")
    args = parser.parse_args()
    settings = {"RPC_RELAY_URL": "", "RPC_RELAY_ADMIN_KEY": "", "RPC_RELAY_EXPIRES_AT": "0",
                "RPC_DEFAULT_TRANSPORT": "auto"}
    if args.disable:
        settings.update(RPC_RELAY_PROVISIONER_URL="", RPC_RELAY_PROVISIONER_KEY="",
                        RPC_CLOUD_RELAY_ENABLED="false")
    if args.idle_timeout_seconds is not None:
        settings["RPC_IDLE_TIMEOUT_SEC"] = str(args.idle_timeout_seconds)
    if not args.disable:
        settings.update(RPC_CLOUD_RELAY_ENABLED="true", RPC_DEFAULT_TRANSPORT="relay")
        outputs = json.loads(subprocess.check_output([
            "terraform", f"-chdir={ROOT / 'infra/relay'}", "output", "-json"], text=True))
        deployment = outputs["deployment"]["value"]
        url, key = deployment["url"].rstrip("/"), outputs["control_key"]["value"]
        instance = json.loads(subprocess.check_output([
            "aws", "ec2", "describe-instances", "--region", deployment["region"],
            "--instance-ids", deployment["instance_id"], "--query", "Reservations[0].Instances[0].{LaunchTime:LaunchTime,State:State.Name}",
            "--output", "json"], text=True))
        if instance["State"] != "running":
            raise RuntimeError("The Terraform relay instance must be running")
        expires = datetime.fromisoformat(instance["LaunchTime"].replace("Z", "+00:00")) + timedelta(hours=deployment["max_runtime_hours"])
        if expires < datetime.now(timezone.utc) + timedelta(minutes=15):
            raise RuntimeError("Less than 15 minutes remain before the relay's runtime limit")
        with urllib.request.urlopen(url + "/healthz", timeout=10) as response:
            if json.load(response).get("service") != "swarm-rpc-relay":
                raise RuntimeError("Unexpected service at the relay URL")
        # Validate control credentials without creating cluster state.
        request = urllib.request.Request(url + "/control/clusters/configuration-check",
                                        method="DELETE", headers={"X-API-Key": key})
        with urllib.request.urlopen(request, timeout=10) as response:
            if json.load(response).get("status") != "ok":
                raise RuntimeError("Relay control authentication failed")
        settings.update(RPC_RELAY_URL=url, RPC_RELAY_ADMIN_KEY=key, RPC_RELAY_EXPIRES_AT=str(expires.timestamp()))
        print(json.dumps({"url": url, "instance_id": deployment["instance_id"],
                          "compute_hourly_usd": deployment["compute_hourly_usd"], "expires_at": expires.isoformat()}))

    if not args.apply:
        print("Preview only. Use --apply to write settings; credentials are omitted from output.")
        return 0
    path = ROOT / ".env"
    lines = [line for line in path.read_text().splitlines()
             if line.split("=", 1)[0].strip() not in settings]
    content = "\n".join(lines + [f"{name}={value}" for name, value in settings.items()]) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".env-relay-", dir=ROOT)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("Coordinator settings written privately. Restart the coordinator after active jobs finish.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
