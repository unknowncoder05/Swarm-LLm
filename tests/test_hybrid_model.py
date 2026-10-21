"""One model may use Ollama or a reserved native cluster without mixing leases."""
import asyncio
import time
from unittest.mock import AsyncMock, patch

from scheduling_support import SchedulingFixture


MODEL = "devstral-small-2:24b"


class HybridModelTests(SchedulingFixture):
    def pair(self):
        for name in ("primary", "worker"):
            self.machine(name)
        cluster = self.c._form_rpc_cluster(MODEL, 17, ctx_size=4096)
        cluster.primary_ready, cluster.status = True, "active"
        return cluster

    async def post(self, **extra):
        return await self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"},
            json={"model": MODEL, "messages": [{"role": "user", "content": "hello"}], **extra})

    async def finish_admission(self, **extra):
        task = asyncio.create_task(self.post(**extra))
        try:
            for _ in range(100):
                if self.c.jobs or task.done():
                    break
                await asyncio.sleep(.005)
            self.assertEqual(len(self.c.jobs), 1)
            job = next(iter(self.c.jobs.values()))
            job.result = {"choices": [{"message": {"content": "Hello"}}]}
            job.done_event.set()
            self.assertEqual((await task).status_code, 200)
            return job
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_two_small_gpus_admit_distributed_devstral_and_keep_sampling(self):
        self.machine("primary")
        self.machine("worker")
        job = await self.finish_admission(rpc_transport="direct")
        cluster = self.c.rpc_clusters[job.rpc_cluster_id]
        self.assertEqual({cluster.primary_id, *cluster.worker_ids}, {"primary", "worker"})
        self.assertEqual(job.body["options"]["temperature"], .15)
        self.assertEqual(job.body["options"]["num_ctx"], 4096)

    async def test_large_single_gpu_keeps_ordinary_ollama_path(self):
        self.machine("large", 24)
        job = await self.finish_admission()
        self.assertIsNone(job.rpc_cluster_id)
        self.assertFalse(self.c.rpc_clusters)

    async def test_explicit_large_context_can_use_pair_instead_of_reducing_context(self):
        self.machine("medium", 16)
        self.machine("small", 12)
        job = await self.finish_admission(options={"num_ctx": 8192}, rpc_transport="direct")
        self.assertIsNotNone(job.rpc_cluster_id)
        self.assertEqual(job.body["options"]["num_ctx"], 8192)

    async def test_new_large_gpu_cannot_steal_distributed_work_or_expose_it_as_download_demand(self):
        cluster = self.pair()
        self.machine("new", 24)
        job = self.enqueue(MODEL, 4096)
        job.rpc_cluster_id = cluster.cluster_id
        response = await self.client.get("/agent/jobs/queued-models?agent_id=new", headers=self.headers)
        self.assertEqual(response.json()["models"], [])
        self.assertEqual((await self.client.get("/agent/jobs/peek", params={"model": MODEL}, headers=self.headers)).status_code, 204)
        self.assertEqual((await self.claim("new", MODEL)).status_code, 204)
        self.assertEqual((await self.claim(cluster.worker_ids[0], MODEL)).status_code, 204)
        self.assertEqual((await self.claim(cluster.primary_id, MODEL)).json()["id"], job.id)

    async def test_native_primary_cannot_steal_ordinary_same_model_job_and_stop_does_not_cancel_it(self):
        cluster = self.pair()
        self.machine("large", 24)
        job = self.enqueue(MODEL, 2048)
        self.assertEqual((await self.claim(cluster.primary_id, MODEL)).status_code, 204)
        response = await self.client.post(f"/rpc/clusters/{cluster.cluster_id}/stop", headers={"X-API-Key": "admin"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(job.cancelled)
        self.assertEqual((await self.claim("large", MODEL)).json()["id"], job.id)

    async def test_recovery_uses_fallback_catalog_and_never_advertises_job_to_ordinary_agents(self):
        job = self.enqueue(MODEL)
        job.rpc_recovering = True
        self.machine("new", 24)
        with patch.object(self.c, "_form_automatic_rpc_cluster", new=AsyncMock(return_value=None)) as form:
            await self.c._recover_rpc_jobs_once()
        self.assertEqual(form.call_args.args[:2], (MODEL, 17.0))
        self.assertEqual((await self.claim("new", MODEL)).status_code, 204)
        response = await self.client.get("/agent/jobs/queued-models?agent_id=new", headers=self.headers)
        self.assertEqual(response.json()["models"], [])

    async def test_recovering_requests_share_forming_replica_and_keep_original_relay_budget(self):
        cluster = self.pair()
        cluster.status, cluster.primary_ready, cluster.transport = "forming", False, "relay"
        first, second = self.enqueue(MODEL), self.enqueue(MODEL)
        for job in (first, second):
            job.rpc_recovering = True
            job.rpc_cluster_id = "old-removed-cluster"
        with patch.object(self.c, "_form_automatic_rpc_cluster", new=AsyncMock()) as form:
            await self.c._recover_rpc_jobs_once()
            form.assert_not_awaited()
        for job in (first, second):
            self.assertEqual(job.rpc_cluster_id, cluster.cluster_id)
            self.assertFalse(job.rpc_recovering)
            self.assertEqual(job.rpc_timeout, 2 * self.c.JOB_TIMEOUT)
        self.assertEqual(len(self.c._queued_chat_jobs()), 2)
        first.rpc_recovering = True
        first.rpc_cluster_id = "old-removed-cluster"
        first.created_at = time.time() - self.c.JOB_TIMEOUT - 1
        await self.c._recover_rpc_jobs_once()
        self.assertFalse(first.cancelled)
        first.rpc_recovering = True
        first.rpc_cluster_id = "old-removed-cluster"
        first.created_at = time.time() - 2 * self.c.JOB_TIMEOUT - 1
        await self.c._recover_rpc_jobs_once()
        self.assertTrue(first.cancelled)

    async def test_partial_capacity_rejects_without_accepting_work(self):
        self.machine("alone")
        response = await self.post(rpc_transport="direct")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["X-Swarm-Status"], "waiting_for_capacity")
        self.assertFalse(self.c.jobs)
        self.assertFalse(self.c.rpc_clusters)

    async def test_catalog_counts_physical_gpus_once(self):
        first = self.machine("first")
        duplicate = self.machine("duplicate")
        duplicate.lan_ip = first.lan_ip
        async def models():
            response = await self.client.get("/v1/models", headers={"X-API-Key": "consumer"})
            return {item["id"] for item in response.json()["data"]}
        self.assertNotIn(MODEL, await models())
        self.machine("second")
        self.assertIn(MODEL, await models())

    async def test_duplicate_identity_cannot_bypass_reservation_for_older_qwen_job(self):
        first = self.machine("first")
        duplicate = self.machine("duplicate")
        duplicate.lan_ip = first.lan_ip
        self.machine("second")
        self.enqueue("qwen3:14b")
        self.assertIsNone(self.c._form_rpc_cluster(MODEL, 17))
        self.assertFalse(any(m.in_rpc for m in self.c.machines.values()))

    async def test_qwen_waits_without_accepting_work_while_devstral_owns_pair(self):
        self.pair()
        response = await self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"},
            json={"model": "qwen3:14b", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["X-Swarm-Status"], "waiting_for_capacity")
        self.assertFalse(self.c.jobs)

    async def test_qwen_thinking_reaches_ollama_at_top_level_and_preserves_opt_in(self):
        self.machine("gpu")
        for extra, expected in (({}, False), ({"think": True}, True),
                                ({"options": {"think": True}}, True),
                                ({"think": False, "options": {"think": True}}, False)):
            self.c.jobs.clear()
            task = asyncio.create_task(self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"},
                json={"model": "qwen3:8b", "messages": [{"role": "user", "content": "hello"}], **extra}))
            try:
                for _ in range(100):
                    if self.c.jobs or task.done(): break
                    await asyncio.sleep(.005)
                job = next(iter(self.c.jobs.values()))
                self.assertIs(job.body["think"], expected)
                self.assertNotIn("think", job.body["options"])
                job.result = {"choices": [{"message": {"content": "hello"}}]}
                job.done_event.set()
                self.assertEqual((await task).status_code, 200)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_explicit_ollama_thinking_is_exposed_as_openai_reasoning(self):
        self.machine("gpu")
        job = self.enqueue("qwen3:8b")
        claim = await self.claim("gpu", job.model)
        response = await self.client.post(f"/agent/jobs/{job.id}/result", headers=self.headers,
            json={"claim_token": claim.json()["claim_token"], "result": {
                "model": job.model, "done_reason": "stop", "message": {
                    "role": "assistant", "thinking": "Arithmetic reasoning", "content": "56"}}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(job.result["choices"][0]["message"]["reasoning_content"], "Arithmetic reasoning")
