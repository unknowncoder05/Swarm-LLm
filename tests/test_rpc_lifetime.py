"""Keep coding sessions warm without extending an expiring relay's lifetime."""
import asyncio
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from test_agent_reports import ROOT, load


class RpcLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"ADMIN_KEY": "admin", "AGENT_KEY": "agent", "CONSUMER_KEY": "consumer",
                                     "RPC_RELAY_URL": "", "RPC_RELAY_ADMIN_KEY": "", "RPC_IDLE_TIMEOUT_SEC": "900"}):
            self.c = load("rpc_lifetime_coordinator", ROOT / "coordinator/coordinator.py")
        self.c.DB_PATH = str(Path(self.temp.name) / "db.sqlite")
        self.c._init_db()
        for index in (1, 2):
            reg = self.c.RegisterPayload(agent_id=f"gpu-{index}", hostname=f"gpu-{index}", port=11434,
                model="qwen3:14b", gpu_vram_gb=12, lan_ip=f"10.0.0.{index}")
            self.c.machines[reg.agent_id] = self.c.MachineState("same-nat", reg)
        self.cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19, ctx_size=32768, transport="relay")
        self.cluster.status, self.cluster.primary_ready = "active", True
        self.cluster.last_job_at = time.time() - 360

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def tick(self):
        with patch.object(self.c.asyncio, "sleep", side_effect=[None, asyncio.CancelledError()]), \
             patch.object(self.c, "_regional_relay_call", return_value={}):
            with self.assertRaises(asyncio.CancelledError):
                await self.c._rpc_lifecycle_loop()

    async def test_fifteen_minute_policy_keeps_model_after_old_five_minute_limit(self):
        await self.tick()
        self.assertEqual(self.cluster.status, "active")
        self.assertEqual(self.cluster.to_dict()["idle_timeout_sec"], 900)
        self.assertEqual(self.c.agent_rpc_assignments["gpu-1"]["ctx_size"], 32768)

    async def test_idle_model_is_released_after_configured_timeout(self):
        self.cluster.last_job_at = time.time() - 901
        await self.tick()
        self.assertEqual(self.cluster.dissolve_reason, "idle")
        self.assertTrue(self.c.agent_rpc_assignments["gpu-1"]["dissolve"])

    async def test_busy_generation_and_waiting_jobs_are_never_idle_unloaded(self):
        self.cluster.last_job_at = time.time() - 1800
        self.c.machines["gpu-1"].busy = True
        await self.tick()
        self.assertEqual(self.cluster.status, "active")
        self.c.machines["gpu-1"].busy = False
        job = self.c.Job({}, "qwen3:30b-a3b")
        self.c.jobs[job.id] = job
        await self.tick()
        self.assertEqual(self.cluster.status, "active")

    async def test_warm_lifetime_does_not_hold_idle_regional_cluster_near_budget_deadline(self):
        self.cluster.relay_url = "https://regional"
        self.c.RPC_RELAY_EXPIRES_AT = time.time() + 240
        self.c.RPC_IDLE_TIMEOUT = 3600
        self.assertEqual(self.cluster.to_dict()["relay_expires_at"], self.c.RPC_RELAY_EXPIRES_AT)
        await self.tick()
        self.assertEqual(self.cluster.dissolve_reason, "relay_deadline")

    async def test_deadline_drain_keeps_running_job_and_rejects_new_admissions(self):
        self.cluster.relay_url = "https://regional"
        self.c.RPC_RELAY_EXPIRES_AT = time.time() + 240
        self.c.machines["gpu-1"].busy = True
        await self.tick()
        self.assertEqual(self.cluster.status, "active")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.c.app), base_url="http://test") as client:
            response = await client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"}, json={
                "model": "qwen3:30b-a3b", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "30")
        self.assertFalse(self.c.jobs)

    def test_invalid_idle_lifetimes_fail_startup(self):
        for value in ("59", "3601", "bad", "1.5"):
            with self.subTest(value=value), patch.dict(os.environ, {
                "ADMIN_KEY": "admin", "AGENT_KEY": "agent", "CONSUMER_KEY": "consumer",
                "RPC_IDLE_TIMEOUT_SEC": value}), self.assertRaisesRegex(RuntimeError, "RPC_IDLE_TIMEOUT_SEC"):
                load("invalid_rpc_lifetime", ROOT / "coordinator/coordinator.py")


if __name__ == "__main__":
    unittest.main()
