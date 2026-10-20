"""Coordinator provisioning must honor GPU eligibility and current reservations."""
import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

from scheduling_support import SchedulingFixture


class ManagedRelayTests(SchedulingFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.deployment = {"url": "https://relay.example", "control_key": "private-generated-control",
                           "expires_at": time.time() + 14000}
        self.c.relay_provisioner = Mock(ensure=AsyncMock(return_value=self.deployment), renew=AsyncMock())
        self.c.RPC_RELAY_EXPIRES_AT = time.time() - 1000

    def pair(self):
        for name in ("primary", "worker"):
            self.machine(name).runtime = {"rpc_relay_external": True}

    async def test_concurrent_first_use_creates_one_cluster_with_dynamic_credentials(self):
        self.pair()
        results = await asyncio.gather(*(self.c._form_managed_rpc_cluster("qwen3:30b-a3b", 19, 32768) for _ in range(4)))
        cluster = results[0][0]
        self.assertTrue(results[0][1]); self.assertTrue(all(not r[1] for r in results[1:]))
        self.c.relay_provisioner.ensure.assert_awaited_once()
        self.assertEqual(len(self.c.rpc_clusters), 1)
        self.assertEqual(cluster.relay_deadline, self.deployment["expires_at"])
        self.assertEqual(cluster.relay_control_key, self.deployment["control_key"])
        self.assertNotIn("private-generated-control", str(cluster.to_dict()))
        self.assertTrue(all(a["relay_url"] == self.deployment["url"] for a in self.c.agent_rpc_assignments.values()))

    async def test_sufficient_single_gpu_does_not_start_a_relay(self):
        self.machine("large", vram=24)
        cluster, created = await self.c._form_managed_rpc_cluster("qwen3:30b-a3b", 19, 32768)
        self.assertTrue(created); self.assertFalse(cluster.worker_ids); self.assertIsNone(cluster.relay_url)
        self.c.relay_provisioner.ensure.assert_not_awaited()

    async def test_insufficient_capacity_or_legacy_agents_do_not_start_resources(self):
        self.machine("first")
        cluster, _ = await self.c._form_managed_rpc_cluster("qwen3:30b-a3b", 19, 32768)
        self.assertIsNone(cluster)
        self.machine("second")
        cluster, _ = await self.c._form_managed_rpc_cluster("qwen3:30b-a3b", 19, 32768)
        self.assertIsNone(cluster); self.c.relay_provisioner.ensure.assert_not_awaited()

    async def test_provisioner_failure_returns_retry_without_reserving_gpus(self):
        self.pair(); self.c.relay_provisioner.ensure.side_effect = RuntimeError("credentials must not leak")
        response = await self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"}, json={
            "model": "qwen3:30b-a3b", "rpc_transport": "relay", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(response.status_code, 503); self.assertEqual(response.headers["retry-after"], "30")
        self.assertNotIn("credentials", response.text)
        self.assertFalse(self.c.rpc_clusters); self.assertFalse(self.c.jobs)
        self.assertTrue(all(not m.in_rpc for m in self.c.machines.values()))

    async def test_capacity_is_rechecked_after_slow_provisioning(self):
        self.pair(); entered, finish = asyncio.Event(), asyncio.Event()
        async def provision():
            entered.set(); await finish.wait(); return self.deployment
        self.c.relay_provisioner.ensure.side_effect = provision
        task = asyncio.create_task(self.c._form_managed_rpc_cluster("qwen3:30b-a3b", 19, 32768))
        await entered.wait()
        self.assertFalse(self.c.agent_rpc_assignments)
        self.enqueue(); self.assertEqual((await self.claim("worker")).status_code, 200)
        finish.set(); cluster, _ = await task
        self.assertIsNone(cluster)
        self.assertTrue(self.c.machines["worker"].busy)
        self.assertFalse(self.c.machines["primary"].in_rpc)

    async def test_cancelled_startup_never_assigns_gpu_work(self):
        self.pair(); entered = asyncio.Event()
        async def provision():
            entered.set(); await asyncio.Future()
        self.c.relay_provisioner.ensure.side_effect = provision
        task = asyncio.create_task(self.c._form_managed_rpc_cluster("qwen3:30b-a3b", 19, 32768))
        await entered.wait(); task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.c.rpc_clusters); self.assertFalse(self.c.agent_rpc_assignments)

    async def test_disconnected_http_client_after_boot_does_not_start_gpu_processes(self):
        self.pair()
        request = Mock(is_disconnected=AsyncMock(side_effect=[False, True]))
        with self.assertRaises(self.c.HTTPException) as error:
            await self.c._form_managed_rpc_cluster("qwen3:30b-a3b", 19, 32768, request)
        self.assertEqual(error.exception.status_code, 499)
        self.c.relay_provisioner.ensure.assert_awaited_once()
        self.assertFalse(self.c.rpc_clusters); self.assertFalse(self.c.agent_rpc_assignments)

    async def test_different_context_waiting_on_same_startup_gets_conflict(self):
        self.pair()
        await self.c._form_managed_rpc_cluster("qwen3:30b-a3b", 19, 16384)
        with self.assertRaises(self.c.HTTPException) as error:
            await self.c._form_managed_rpc_cluster("qwen3:30b-a3b", 19, 32768)
        self.assertEqual(error.exception.status_code, 409)

    async def test_expired_provisioner_renewals_drain_before_cloud_cleanup(self):
        self.pair(); cluster, _ = await self.c._form_managed_rpc_cluster("qwen3:30b-a3b", 19, 32768)
        cluster.provisioner_last_ok = time.time() - 121
        self.c.relay_provisioner.renew.side_effect = RuntimeError("unreachable")
        with patch.object(self.c.asyncio, "sleep", side_effect=[None, asyncio.CancelledError()]), \
             patch.object(self.c, "_regional_relay_call", return_value={}):
            with self.assertRaises(asyncio.CancelledError): await self.c._rpc_lifecycle_loop()
        self.assertEqual(cluster.dissolve_reason, "provisioner_unreachable")
