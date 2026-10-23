#!/usr/bin/env python3
"""Apply this lab, exercise it through SSM, save evidence, and destroy in finally."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent


def interrupt_for_cleanup(signum, _frame):
    raise KeyboardInterrupt("Received " + signal.Signals(signum).name)


def terraform(*args, capture=False):
    command = ["terraform", "-chdir=" + str(HERE)] + list(args)
    if capture:
        return json.loads(subprocess.check_output(command, text=True))
    subprocess.run(command, check=True)


def aws(region, *args):
    return json.loads(subprocess.check_output(
        ["aws", "--region", region] + list(args) + ["--output", "json"], text=True))


def send(region, instance, commands, timeout=120):
    result = aws(region, "ssm", "send-command", "--instance-ids", instance,
                 "--document-name", "AWS-RunShellScript",
                 "--parameters", json.dumps({"commands": commands, "executionTimeout": [str(timeout)]}))
    return result["Command"]["CommandId"]


def invocation(region, instance, command_id):
    return aws(region, "ssm", "get-command-invocation", "--instance-id", instance,
               "--command-id", command_id)


def wait_command(region, instance, command_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = invocation(region, instance, command_id)
        except subprocess.CalledProcessError:
            time.sleep(5)
            continue
        if result["Status"] not in ("Pending", "InProgress", "Delayed"):
            return result
        time.sleep(10)
    raise TimeoutError("SSM command exceeded local wait deadline")


def wait_ready(lab, timeout=4500):
    region = lab["region"]
    ids = [lab["coordinator_id"]] + lab["gpu_ids"]
    checks = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        online = aws(region, "ssm", "describe-instance-information", "--filters",
                     json.dumps([{"Key": "InstanceIds", "Values": ids}]))
        online_ids = {node["InstanceId"] for node in online["InstanceInformationList"]
                      if node["PingStatus"] == "Online"}
        for instance in ids:
            if instance not in online_ids:
                continue
            if instance not in checks:
                checks[instance] = send(region, instance, [
                    "set -eu",
                    "cloud-init status --wait >/dev/null",
                    "systemctl is-active --quiet peerfoundry-lab.service",
                    "test -s /opt/peerfoundry/config.json",
                ], timeout=timeout)
            try:
                result = invocation(region, instance, checks[instance])
            except subprocess.CalledProcessError:
                continue
            if result["Status"] in ("Failed", "Cancelled", "TimedOut"):
                raise RuntimeError("Bootstrap failed on {}: {}".format(instance, result.get("StandardErrorContent")))
        if len(checks) == len(ids):
            statuses = [invocation(region, instance, checks[instance])["Status"] for instance in ids]
            if all(status == "Success" for status in statuses):
                print("Bootstrap complete on all machines.", flush=True)
                # Allow registration and early heartbeats to reach the coordinator.
                time.sleep(20)
                return
        print("Waiting for GPU build, model download, and agent registration...", flush=True)
        time.sleep(30)
    raise TimeoutError("Bootstrap exceeded 75 minutes; inspect captured logs")


def evidence(lab):
    results = {}
    commands = {}
    for instance in [lab["coordinator_id"]] + lab["gpu_ids"]:
        try:
            commands[instance] = send(lab["region"], instance, [
                "echo '--- devices ---'; cat /var/log/peerfoundry-devices.log 2>/dev/null || true",
                "echo '--- GPU allocations ---'; nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv 2>/dev/null || true",
                "echo '--- GPU processes ---'; nvidia-smi --query-compute-apps=process_name,used_gpu_memory --format=csv,noheader,nounits 2>/dev/null || true",
                "echo '--- device buffers ---'; grep -E 'CUDA|RPC|offload|buffer|split' /var/log/peerfoundry-llama.log 2>/dev/null | tail -40 || true",
                "echo '--- llama allocations ---'; tail -65 /var/log/peerfoundry-llama.log 2>/dev/null || true",
                "echo '--- RPC allocations ---'; tail -30 /var/log/swarm-rpc.log 2>/dev/null || true",
                # Keep proof first: SSM truncates large command output.
                "echo '--- bootstrap ---'; tail -30 /var/log/peerfoundry-bootstrap.log | tr '\\r' '\\n' | tail -30 | cut -c1-240",
                "echo '--- service ---'; journalctl -u peerfoundry-lab --no-pager -n 35",
                "echo '--- Ollama preparation ---'; tail -35 /var/log/peerfoundry-ollama-prepare.log 2>/dev/null | cut -c1-240 || true",
            ])
        except Exception as exc:
            results[instance] = {"error": str(exc)}
    for instance, command_id in commands.items():
        try:
            results[instance] = wait_command(lab["region"], instance, command_id, 180)
        except Exception as exc:
            results[instance] = {"error": str(exc)}
    return results


def verify_gpu_evidence(lab, observations):
    allocated = {}
    for instance in lab["gpu_ids"]:
        result = observations.get(instance, {})
        output = result.get("StandardOutputContent", "")
        section = output.partition("--- GPU processes ---")[2].partition("--- device buffers ---")[0]
        processes = []
        for line in section.splitlines():
            name, separator, memory = line.rpartition(",")
            if separator and Path(name.strip()).name in ("llama-server", "ggml-rpc-server"):
                try:
                    if float(memory.strip()) >= 1024:
                        processes.append({"name": name.strip(), "memory_mib": float(memory.strip())})
                except ValueError:
                    pass
        if result.get("Status") != "Success" or not processes:
            raise RuntimeError("No substantial llama.cpp GPU allocation proved on " + instance)
        allocated[instance] = processes
    return allocated


def main():
    # Terraform state and diagnostic files can contain credentials or source.
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="Keep resources for inspection; destroy them yourself before the run-time limit.")
    parser.add_argument("--test-existing", action="store_true", help="Use this directory's already-applied lab instead of applying again.")
    parser.add_argument("--apply-plan", type=Path, help="Apply an already-reviewed saved Terraform plan without an interactive prompt.")
    args = parser.parse_args()
    if args.test_existing and args.apply_plan:
        parser.error("--test-existing and --apply-plan are mutually exclusive")
    report = {"started_at": datetime.now(timezone.utc).isoformat()}
    lab = None
    touched = args.test_existing
    failed = False
    try:
        if not args.test_existing:
            terraform("init", "-input=false")
            # Terraform displays the full plan and asks before creating paid GPUs.
            touched = True
            if args.apply_plan:
                terraform("apply", "-input=false", str(args.apply_plan.resolve()))
            else:
                terraform("apply")
        lab = terraform("output", "-json", "lab", capture=True)
        report["lab"] = lab
        wait_ready(lab)
        command_id = send(lab["region"], lab["coordinator_id"], [
            "/opt/peerfoundry/venv/bin/python /opt/peerfoundry/smoke.py",
        ], timeout=2400)
        result = wait_command(lab["region"], lab["coordinator_id"], command_id, 2460)
        report["smoke"] = result
        if result["Status"] != "Success":
            raise RuntimeError("Inference trial failed: " + result.get("StandardErrorContent", ""))
        report["evidence"] = evidence(lab)
        report["gpu_allocations"] = verify_gpu_evidence(lab, report["evidence"])
        report["status"] = "passed"
        print(result["StandardOutputContent"], flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        failed = True
        report["error"] = str(exc) or "Interrupted"
        print("Trial failed: " + report["error"], file=sys.stderr, flush=True)
    finally:
        try:
            if lab and "evidence" not in report:
                report["evidence"] = evidence(lab)
            (HERE / "results.json").write_text(json.dumps(report, indent=2))
        except (Exception, KeyboardInterrupt) as exc:
            failed = True
            print("Could not save all evidence: {}".format(exc), file=sys.stderr)
        finally:
            # A full disk, failed diagnostics, or interrupt must not skip cleanup.
            if touched and not args.keep:
                print("Destroying all Terraform-managed lab resources...", flush=True)
                try:
                    terraform("destroy", "-auto-approve")
                except (Exception, KeyboardInterrupt):
                    failed = True
                    print("Cleanup did not complete. Run terraform -chdir=infra/rpc-lab destroy immediately.", file=sys.stderr)
            elif touched:
                print("Resources retained. Run terraform destroy in this directory when finished.", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    # A closed terminal or normal process termination should enter finally too.
    termination_signals = [signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        termination_signals.append(signal.SIGHUP)
    for termination_signal in termination_signals:
        signal.signal(termination_signal, interrupt_for_cleanup)
    sys.exit(main())
