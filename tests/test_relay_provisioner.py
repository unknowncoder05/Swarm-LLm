"""Budget, idle cleanup, concurrency, restart recovery, and private API boundaries."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import subprocess
import unittest
import urllib.request
import urllib.error
from unittest.mock import Mock, patch

from test_agent_reports import ROOT, load

p = load("relay_provisioner_tests", ROOT / "coordinator/relay_provisioner.py")


class Clock:
    def __init__(self): self.now = 1000
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


class Backend:
    def __init__(self, clock):
        self.clock, self.deployment = clock, None
        self.applied = []
        self.healthy = Mock(return_value=True)
    def describe(self): return self.deployment
    def apply(self, destroy=False, still_needed=None):
        if destroy and still_needed and still_needed(): return False
        self.applied.append(destroy)
        self.deployment = None if destroy else {"url": "https://relay.example", "control_key": "private-control",
            "expires_at": self.clock() + 14400, "instance_id": "small-instance"}
        return True


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.clock = Clock(); self.backend = Backend(self.clock)
        self.path = Path(self.temp.name) / "leases.json"
        self.service = p.RelayLifecycle(self.backend, self.path, idle_seconds=60, clock=self.clock, sleep=self.clock.sleep)

    def test_first_use_applies_and_repeated_use_adopts_same_instance(self):
        first = self.service.ensure(); second = self.service.ensure()
        self.assertEqual(first, second)
        self.assertEqual(self.backend.applied, [False])
        self.assertNotIn("private-control", json.dumps(self.service.status()))

    def test_idle_cleanup_destroys_once_and_next_demand_recreates(self):
        self.service.ensure(); self.clock.sleep(61)
        self.assertTrue(self.service.cleanup()); self.assertFalse(self.service.cleanup())
        self.service.ensure()
        self.assertEqual(self.backend.applied, [False, True, False])

    def test_live_lease_prevents_idle_shutdown_and_survives_service_restart(self):
        self.service.ensure(); self.service.touch("cluster-one")
        self.clock.sleep(70)
        restored = p.RelayLifecycle(self.backend, self.path, 60, self.clock, self.clock.sleep)
        self.assertFalse(restored.cleanup())
        self.clock.sleep(111)
        self.assertTrue(restored.cleanup())

    def test_concurrent_ensure_starts_only_one_instance(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.service.ensure(), range(4)))
        self.assertEqual(self.backend.applied, [False])
        self.assertTrue(all(r == results[0] for r in results))

    def test_new_demand_during_destroy_plan_aborts_cleanup(self):
        self.service.ensure(); self.clock.sleep(61)
        original = self.backend.apply
        def apply(destroy=False, still_needed=None):
            if destroy: self.service.touch("new-cluster")
            return original(destroy, still_needed)
        self.backend.apply = apply
        self.assertFalse(self.service.cleanup())
        self.assertEqual(self.backend.applied, [False])

    def test_failed_bootstrap_releases_startup_hold_and_can_be_cleaned(self):
        self.backend.healthy.return_value = False
        with self.assertRaisesRegex(RuntimeError, "healthy"):
            self.service.ensure()
        self.assertNotIn("startup", self.service.leases)
        self.assertTrue(self.service.cleanup())

    def test_near_deadline_instance_is_preserved_while_an_actual_cluster_is_leased(self):
        self.service.ensure(); self.service.touch("live-cluster")
        self.backend.deployment["expires_at"] = self.clock() + 299
        with self.assertRaisesRegex(RuntimeError, "still leased"):
            self.service.ensure()
        self.assertEqual(self.backend.applied, [False])

    def test_near_deadline_idle_instance_is_replaced_before_use(self):
        self.service.ensure(); self.backend.deployment["expires_at"] = self.clock() + 299
        self.service.ensure()
        self.assertEqual(self.backend.applied, [False, True, False])

    def test_long_boot_finishes_with_a_fresh_idle_grace(self):
        original = self.backend.apply
        def boot(*args, **kwargs):
            self.clock.sleep(600); return original(*args, **kwargs)
        self.backend.apply = boot
        self.service.ensure()
        self.assertFalse(self.service.cleanup())

    def test_new_ensure_waiting_for_destroy_retains_future_idle_cleanup(self):
        self.service.ensure(); self.clock.sleep(61)
        entered, finish, requested = threading.Event(), threading.Event(), threading.Event()
        original = self.backend.apply
        touch = self.service.touch
        def notify_touch(identity, ttl=180):
            touch(identity, ttl)
            if identity == "startup": requested.set()
        self.service.touch = notify_touch
        def apply(destroy=False, still_needed=None):
            if destroy:
                if still_needed and still_needed(): return False
                entered.set(); self.assertTrue(finish.wait(2))
                return original(destroy, lambda: False)
            return original(destroy, still_needed)
        self.backend.apply = apply
        with ThreadPoolExecutor(max_workers=2) as pool:
            cleanup = pool.submit(self.service.cleanup)
            self.assertTrue(entered.wait(2))
            ensure = pool.submit(self.service.ensure)
            self.assertTrue(requested.wait(2))
            finish.set(); self.assertTrue(cleanup.result()); ensure.result()
        self.clock.sleep(61)
        self.assertTrue(self.service.cleanup())

    def test_private_api_auth_and_status_do_not_expose_control_secret(self):
        server = p.make_server(self.service, "a" * 40, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(urllib.request.Request(base + "/ensure", data=b"{}"))
        self.assertEqual(raised.exception.code, 401); self.assertEqual(self.backend.applied, [])
        headers = {"X-API-Key": "a" * 40}
        with urllib.request.urlopen(urllib.request.Request(base + "/ensure", data=b"{}", headers=headers)) as response:
            self.assertEqual(json.load(response)["control_key"], "private-control")
        with urllib.request.urlopen(urllib.request.Request(base + "/status", headers=headers)) as response:
            self.assertNotIn("private-control", response.read().decode())


class PlanGuardTests(unittest.TestCase):
    def plan(self):
        return {"resource_changes": [{"mode": "managed", "address": "aws_instance.relay", "type": "aws_instance", "change": {"after": {
            "instance_type": "t4g.micro", "credit_specification": [{"cpu_credits": "standard"}],
            "root_block_device": [{"volume_size": 8, "volume_type": "gp3", "encrypted": True, "delete_on_termination": True}]}}}],
            "output_changes": {"deployment": {"after": {"region": "eu-south-2", "compute_hourly_usd": .0092, "max_runtime_hours": 4}}}}

    def test_approved_small_default_profile_passes(self): p.TerraformRelay.validate_plan(self.plan())

    def test_cleanup_can_remove_its_owned_instance_even_if_someone_resized_it(self):
        plan = self.plan(); resource = plan["resource_changes"][0]
        resource["change"] = {"after": None, "before": {"instance_type": "g5.xlarge"}, "actions": ["delete"]}
        p.TerraformRelay.validate_plan(plan, destroy=True)
        with self.assertRaises(RuntimeError): p.TerraformRelay.validate_plan(plan)

    def test_unexpected_resources_large_instance_and_unlimited_credits_are_rejected(self):
        for mutation in (lambda r: r.update(address="aws_nat_gateway.new"),
                         lambda r: r["change"]["after"].update(instance_type="g5.xlarge"),
                         lambda r: r["change"]["after"].update(credit_specification=[{"cpu_credits": "unlimited"}]),
                         lambda r: r["change"]["after"]["root_block_device"][0].update(volume_size=100)):
            plan = self.plan(); mutation(plan["resource_changes"][0])
            with self.assertRaises(RuntimeError): p.TerraformRelay.validate_plan(plan)

    def test_price_region_and_runtime_cannot_exceed_approved_limits(self):
        for key, value in (("region", "us-east-1"), ("compute_hourly_usd", .03), ("max_runtime_hours", 8)):
            plan = self.plan(); plan["output_changes"]["deployment"]["after"][key] = value
            with self.assertRaises(RuntimeError): p.TerraformRelay.validate_plan(plan)

    def test_missing_expired_instance_is_recreatable_but_auth_errors_are_not_hidden(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "infra/relay").mkdir(parents=True)
            backend = p.TerraformRelay(Path(temp))
            with patch.object(p.subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "", "InvalidInstanceID.NotFound")):
                self.assertEqual(json.loads(backend.run(["aws"], missing_instance_ok=True)), {"Reservations": []})
            with patch.object(p.subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "", "AccessDenied private-detail")):
                with self.assertRaisesRegex(RuntimeError, "private provisioner log") as error:
                    backend.run(["aws"], missing_instance_ok=True)
                self.assertNotIn("private-detail", str(error.exception))
