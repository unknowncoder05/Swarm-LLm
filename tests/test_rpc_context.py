"""RPC context must reach native startup and cannot silently reuse a smaller cluster."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from test_agent_reports import ROOT, load


class RpcContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"ADMIN_KEY": "admin", "AGENT_KEY": "agent", "CONSUMER_KEY": "consumer",
                                     "RPC_RELAY_URL": "", "RPC_RELAY_ADMIN_KEY": ""}):
            self.c = load("rpc_context_coordinator", ROOT / "coordinator/coordinator.py")
        self.c.DB_PATH = str(Path(self.temp.name) / "db.sqlite")
        self.c._init_db()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.c.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def post(self, **extra):
        return await self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"},
            json={"model": "qwen3:30b-a3b", "messages": [{"role": "user", "content": "hello"}], **extra})

    async def complete(self, **extra):
        task = asyncio.create_task(self.post(**extra))
        try:
            for _ in range(100):
                if self.c.jobs or task.done():
                    break
                await asyncio.sleep(.001)
            self.assertTrue(self.c.jobs, "Request did not create a job")
            job = next(iter(self.c.jobs.values()))
            job.result = {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
            job.done_event.set()
            response = await asyncio.wait_for(task, timeout=2)
            self.assertEqual(response.status_code, 200, response.text)
            return job
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_32k_request_forms_two_gpu_cluster_with_matching_native_assignment(self):
        for number in (1, 2):
            response = await self.client.post("/register", headers={"X-API-Key": "agent"}, json={
                "agent_id": f"gpu-{number}", "port": 11434, "model": "qwen3:14b", "gpu_vram_gb": 12,
                "lan_ip": f"10.0.0.{number}"})
            self.assertEqual(response.status_code, 200)
        job = await self.complete(options={"num_ctx": 32768}, rpc_transport="relay")
        cluster = next(iter(self.c.rpc_clusters.values()))
        self.assertEqual(len(cluster.worker_ids), 1)
        self.assertEqual(cluster.to_dict()["ctx_size"], 32768)
        self.assertEqual(self.c.agent_rpc_assignments[cluster.primary_id]["ctx_size"], 32768)
        self.assertEqual(job.body["options"]["num_ctx"], 32768)

    async def test_larger_request_cannot_silently_reuse_16k_cluster(self):
        cluster = self.c.RpcCluster("existing", "qwen3:30b-a3b", "primary", ["worker"], ctx_size=16384)
        self.c.rpc_clusters["existing"] = cluster
        response = await self.post(options={"num_ctx": 32768})
        self.assertEqual(response.status_code, 409)
        self.assertIn("16384", response.json()["detail"])
        self.assertFalse(self.c.jobs)
        self.assertEqual(cluster.status, "forming")

    async def test_bare_30b_request_keeps_32k_default_instead_of_creating_small_cluster(self):
        for number in (1, 2):
            await self.client.post("/register", headers={"X-API-Key": "agent"}, json={
                "agent_id": f"gpu-{number}", "port": 11434, "model": "qwen3:14b", "gpu_vram_gb": 12,
                "lan_ip": f"10.0.0.{number}"})
        job = await self.complete(rpc_transport="relay")
        cluster = next(iter(self.c.rpc_clusters.values()))
        self.assertEqual(cluster.ctx_size, 32768)
        self.assertEqual(job.body["options"]["num_ctx"], 32768)

    async def test_omitted_context_uses_existing_cluster_context(self):
        self.c.rpc_clusters["existing"] = self.c.RpcCluster(
            "existing", "qwen3:30b-a3b", "primary", ["worker"], ctx_size=32768)
        job = await self.complete()
        self.assertEqual(job.body["options"]["num_ctx"], 32768)

    async def test_invalid_context_is_rejected_before_queue_even_with_existing_cluster(self):
        for existing in (False, True):
            if existing:
                self.c.rpc_clusters["existing"] = self.c.RpcCluster(
                    "existing", "qwen3:30b-a3b", "primary", ["worker"], ctx_size=32768)
            for context in (True, 511, 32769, 32768.0, "32768", None):
                with self.subTest(existing=existing, context=context):
                    response = await self.post(options={"num_ctx": context})
                    self.assertEqual(response.status_code, 422)
                    self.assertFalse(self.c.jobs)
            for options in (None, [], "invalid"):
                response = await self.post(options=options)
                self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
