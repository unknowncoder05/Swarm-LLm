"""Local, authenticated Terraform lifecycle service. AWS credentials stay here."""
from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request


class TerraformRelay:
    ALLOWED = {
        "aws_instance.relay", "aws_route53_record.relay", "random_password.control",
        "aws_ssm_parameter.keys", "aws_iam_role.relay", "aws_iam_role_policy.keys",
        "aws_iam_role_policy_attachment.ssm", "aws_iam_instance_profile.relay",
        "aws_security_group.relay", "aws_vpc_security_group_ingress_rule.acme",
        "aws_vpc_security_group_egress_rule.outbound",
    }

    def __init__(self, root, clock=time.time):
        self.module = Path(root) / "infra/relay"
        self.clock = clock
        self.logs = self.module / ".provisioner"
        self.logs.mkdir(mode=0o700, exist_ok=True)

    def run(self, command, timeout=300, missing_instance_ok=False):
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                env=dict(os.environ, TF_IN_AUTOMATION="1", TF_INPUT="0"))
        if result.returncode:
            if missing_instance_ok and "InvalidInstanceID.NotFound" in result.stderr:
                return '{"Reservations": []}'
            path = self.logs / "last-error.log"
            path.write_text(result.stdout + result.stderr)
            os.chmod(path, 0o600)
            raise RuntimeError("Terraform/AWS operation failed; inspect the private provisioner log")
        return result.stdout

    def tf(self, *args, timeout=300):
        return self.run([os.environ.get("TERRAFORM_BINARY", "terraform"), f"-chdir={self.module}", *args], timeout)

    @classmethod
    def validate_plan(cls, plan, destroy=False):
        for resource in plan.get("resource_changes", []):
            if resource["mode"] != "managed":
                continue
            address = resource["address"]
            if address not in cls.ALLOWED and not address.startswith('aws_vpc_security_group_ingress_rule.https['):
                raise RuntimeError("Unapproved resource in relay plan")
            values = resource["change"].get("after") or resource["change"].get("before") or {}
            if resource["type"] == "aws_instance" and not destroy:
                disks = values.get("root_block_device") or []
                if (values.get("instance_type") != "t4g.micro" or len(disks) != 1
                        or disks[0].get("volume_size") != 8 or not disks[0].get("encrypted")
                        or disks[0].get("volume_type") != "gp3"
                        or not disks[0].get("delete_on_termination")
                        or values.get("credit_specification") != [{"cpu_credits": "standard"}]):
                    raise RuntimeError("Relay plan exceeds the approved small-instance profile")
        if not destroy:
            output = (plan.get("output_changes", {}).get("deployment", {}).get("after")
                      or plan.get("planned_values", {}).get("outputs", {}).get("deployment", {}).get("value") or {})
            if (output.get("region") != "eu-south-2"
                    or not 0 < output.get("compute_hourly_usd", 1) <= .02
                    or not 1 <= output.get("max_runtime_hours", 99) <= 4):
                raise RuntimeError("Relay plan exceeds its region, hourly price, or runtime limits")

    def apply(self, destroy=False, still_needed=None):
        self.tf("init", "-input=false", "-lockfile=readonly")
        path = self.logs / "lifecycle.tfplan"
        self.tf("plan", "-input=false", "-lock-timeout=120s", *(["-destroy"] if destroy else []), f"-out={path}")
        os.chmod(path, 0o600)
        plan = json.loads(self.tf("show", "-json", str(path)))
        self.validate_plan(plan, destroy)
        # A request arriving during the destroy plan cancels idle cleanup.
        if destroy and still_needed and still_needed():
            return False
        self.tf("apply", "-input=false", "-lock-timeout=120s", str(path), timeout=900)
        path.unlink(missing_ok=True)
        return True

    def describe(self):
        outputs = json.loads(self.tf("output", "-json"))
        if "deployment" not in outputs:
            return None
        deployment = dict(outputs["deployment"]["value"])
        raw = self.run(["aws", "ec2", "describe-instances", "--region", "eu-south-2",
                        "--instance-ids", deployment["instance_id"], "--output", "json"], missing_instance_ok=True)
        instances = [i for r in json.loads(raw).get("Reservations", []) for i in r["Instances"]]
        if not instances or instances[0]["State"]["Name"] != "running":
            return None
        deployment["expires_at"] = (datetime.fromisoformat(instances[0]["LaunchTime"].replace("Z", "+00:00")).timestamp()
                                     + deployment["max_runtime_hours"] * 3600)
        deployment["control_key"] = outputs["control_key"]["value"]
        return deployment

    @staticmethod
    def healthy(deployment):
        try:
            url = deployment["url"].rstrip("/")
            with urllib.request.urlopen(url + "/healthz", timeout=3) as response:
                if json.load(response).get("service") != "swarm-rpc-relay":
                    return False
            req = urllib.request.Request(url + "/control/clusters/provisioner-health", method="DELETE",
                                         headers={"X-API-Key": deployment["control_key"]})
            with urllib.request.urlopen(req, timeout=3) as response:
                return json.load(response).get("status") == "ok"
        except Exception:
            return False


class RelayLifecycle:
    def __init__(self, backend, state_path, idle_seconds=300, clock=time.time, sleep=time.sleep):
        if not 60 <= idle_seconds <= 900:
            raise ValueError("Relay cleanup idle lifetime must be between 60 and 900 seconds")
        self.backend, self.path = backend, Path(state_path)
        self.idle_seconds, self.clock, self.sleep = idle_seconds, clock, sleep
        self.state_lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.last_needed, self.leases = clock(), {}
        self.phase, self.deployment = "idle", None
        self.cleaned = False
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            self.last_needed, self.leases = saved["last_needed"], saved["leases"]

    def persist(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            os.chmod(temporary, 0o600)
            json.dump({"last_needed": self.last_needed, "leases": self.leases}, stream)
        temporary.replace(self.path)

    def touch(self, identity, ttl=180):
        if identity != "startup" and not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", identity):
            raise ValueError("Invalid lease identity")
        with self.state_lock:
            self.leases = {k: v for k, v in self.leases.items() if v > self.clock()}
            if identity not in self.leases and len(self.leases) >= 64:
                raise ValueError("Too many relay leases")
            self.last_needed = self.clock()
            self.leases[identity] = self.clock() + ttl
            self.persist()

    def needed(self):
        with self.state_lock:
            return self.clock() - self.last_needed < self.idle_seconds or any(v > self.clock() for v in self.leases.values())

    def ensure(self):
        self.touch("startup", ttl=900)
        with self.operation_lock:
            self.cleaned = False
            self.phase = "starting"
            try:
                deployment = self.backend.describe()
                if deployment and deployment["expires_at"] <= self.clock() + 300:
                    with self.state_lock:
                        if any(v > self.clock() for k, v in self.leases.items() if k != "startup"):
                            raise RuntimeError("The relay is near its deadline and still leased")
                    self.backend.apply(destroy=True, still_needed=lambda: False)
                    deployment = None
                if not deployment:
                    self.backend.apply()
                deadline = self.clock() + 600
                while self.clock() < deadline:
                    deployment = self.backend.describe()
                    if (deployment and deployment["expires_at"] > self.clock() + 300
                            and self.backend.healthy(deployment)):
                        self.deployment, self.phase = deployment, "ready"
                        with self.state_lock:
                            self.last_needed = self.clock()
                            self.persist()
                        return deployment
                    self.sleep(5)
                raise RuntimeError("relay did not become healthy within ten minutes")
            except Exception:
                self.phase = "error"
                raise
            finally:
                with self.state_lock:
                    self.leases.pop("startup", None)
                    self.persist()

    def cleanup(self):
        if self.cleaned or self.needed() or not self.operation_lock.acquire(blocking=False):
            return False
        try:
            if self.needed():
                return False
            self.phase = "stopping"
            if self.backend.apply(destroy=True, still_needed=self.needed):
                self.deployment, self.phase = None, "idle"
                self.cleaned = True
                return True
            self.phase = "ready"
            return False
        except Exception:
            self.phase = "error"
            logging.exception("Relay cleanup failed; Terraform state retained for retry")
            return False
        finally:
            self.operation_lock.release()

    def status(self):
        with self.state_lock:
            return {"phase": self.phase, "active_leases": sum(v > self.clock() for v in self.leases.values()),
                    "idle_seconds": self.idle_seconds,
                    "deployment": {k: v for k, v in (self.deployment or {}).items() if k != "control_key"}}


def make_server(lifecycle, key, bind="127.0.0.1", port=8092):
    if not key or len(key) < 32:
        raise ValueError("A dedicated provisioner key of at least 32 characters is required")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def dispatch(self):
            status, body = 200, {}
            if not hmac.compare_digest(self.headers.get("X-API-Key", ""), key):
                status, body = 401, {"error": "Unauthorized"}
            else:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= 4096:
                        raise ValueError("Invalid request size")
                    if length:
                        json.loads(self.rfile.read(length))
                    if self.command == "POST" and self.path == "/ensure":
                        body = lifecycle.ensure()
                    elif self.command == "PUT" and self.path.startswith("/leases/"):
                        lifecycle.touch(self.path[len("/leases/"):])
                        body = {"status": "ok"}
                    elif self.command == "GET" and self.path == "/status":
                        body = lifecycle.status()
                    else:
                        status, body = 404, {"error": "Unknown operation"}
                except (ValueError, json.JSONDecodeError):
                    status, body = 422, {"error": "Invalid request"}
                except Exception:
                    logging.exception("Relay provisioning failed")
                    status, body = 503, {"error": "Relay unavailable; inspect provisioner service"}
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PUT = dispatch

    return ThreadingHTTPServer((bind, port), Handler)


def serve(lifecycle, key, bind="127.0.0.1", port=8092):
    server = make_server(lifecycle, key, bind, port)
    def maintain():
        while True:
            time.sleep(30)
            lifecycle.cleanup()
    threading.Thread(target=maintain, daemon=True).start()
    server.serve_forever()
