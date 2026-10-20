"""Same-model replicas must isolate queues, claims, cancellation and idleness."""
import asyncio
import time
from unittest.mock import patch
from scheduling_support import SchedulingFixture


class ReplicaTests(SchedulingFixture):
    def pair(self, prefix):
        ids = [prefix + "-primary", prefix + "-worker"]
        for identity in ids:
            self.machine(identity).runtime = {"rpc_quic": True}
        cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19, transport="quic", ctx_size=32768, agent_ids=ids)
        cluster.primary_ready = True; cluster.status = "active"
        return cluster

    async def test_each_primary_claims_only_its_replica_jobs_and_duplicate_claims_stay_fenced(self):
        first, second = self.pair("first"), self.pair("second")
        a = self.enqueue(first.model, 32768); a.rpc_cluster_id = first.cluster_id
        b = self.enqueue(first.model, 32768); b.rpc_cluster_id = second.cluster_id
        replies = await asyncio.gather(self.claim(second.primary_id, second.model), self.claim(first.primary_id, first.model))
        self.assertEqual([response.json()["id"] for response in replies], [b.id, a.id])
        self.assertEqual((await self.claim(first.worker_ids[0], first.model)).status_code, 204)
        self.assertEqual((await self.claim(second.worker_ids[0], second.model)).status_code, 204)

    async def test_dissolving_one_pair_does_not_cancel_other_pair_or_its_reservations(self):
        first, second = self.pair("first"), self.pair("second")
        a = self.enqueue(first.model); a.rpc_cluster_id = first.cluster_id
        b = self.enqueue(first.model); b.rpc_cluster_id = second.cluster_id
        unbound = self.enqueue(first.model)
        self.c._dissolve_cluster(first, "member_offline")
        self.assertTrue(a.cancelled)
        self.assertFalse(b.cancelled)
        self.assertFalse(unbound.cancelled)
        self.assertEqual(second.status, "active")
        self.assertTrue(all(self.c.machines[identity].in_rpc for identity in [second.primary_id, *second.worker_ids]))
        self.assertEqual((await self.claim(second.primary_id, second.model)).json()["id"], b.id)

    async def test_normal_admission_selects_least_loaded_ready_replica(self):
        first, second = self.pair("first"), self.pair("second")
        self.assertIs(self.c._find_rpc_cluster_for_model(first.model), first)
        job = self.enqueue(first.model); job.rpc_cluster_id = first.cluster_id
        self.assertIs(self.c._find_rpc_cluster_for_model(first.model), second)
        self.assertIs(self.c._find_rpc_cluster_for_model(first.model, [first.primary_id, *first.worker_ids]), first)
        second.status = "forming"
        self.assertIs(self.c._find_rpc_cluster_for_model(first.model), first)

    async def test_pending_work_on_second_pair_does_not_keep_idle_first_pair_alive(self):
        first, second = self.pair("first"), self.pair("second")
        first.last_job_at = second.last_job_at = time.time() - self.c.RPC_IDLE_TIMEOUT - 1
        job = self.enqueue(first.model); job.rpc_cluster_id = second.cluster_id
        with patch.object(self.c.asyncio, "sleep", side_effect=[None, asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                await self.c._rpc_lifecycle_loop()
        self.assertEqual(first.status, "dissolving")
        self.assertEqual(second.status, "active")
        self.assertFalse(job.cancelled)

    async def test_two_pinned_admissions_can_form_same_model_and_bind_jobs_independently(self):
        for identity in ("one", "two", "three", "four"):
            self.machine(identity).runtime = {"rpc_quic": True}
        async def request(ids):
            return await self.client.post("/v1/chat/completions", headers={"X-API-Key": "admin"}, json={
                "model": "qwen3:30b-a3b", "rpc_transport": "quic", "rpc_agent_ids": ids,
                "options": {"num_ctx": 32768}, "messages": [{"role": "user", "content": "hello"}]})
        tasks = [asyncio.create_task(request(ids)) for ids in (["one", "two"], ["three", "four"])]
        try:
            for _ in range(100):
                if len(self.c.jobs) == 2 or any(task.done() for task in tasks): break
                await asyncio.sleep(.005)
            self.assertEqual(len(self.c.rpc_clusters), 2)
            self.assertEqual(len(self.c.jobs), 2)
            self.assertEqual(len({job.rpc_cluster_id for job in self.c.jobs.values()}), 2)
            for job in self.c.jobs.values():
                job.result = {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
                job.done_event.set()
            responses = await asyncio.gather(*tasks)
            self.assertTrue(all(response.status_code == 200 for response in responses))
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
