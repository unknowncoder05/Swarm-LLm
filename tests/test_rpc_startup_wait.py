"""Capacity rejections may be retried by the UI without duplicating inference."""
import asyncio
from scheduling_support import SchedulingFixture


class RpcStartupWaitTests(SchedulingFixture):
    async def post(self, model="qwen3:30b-a3b", **extra):
        return await self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"},
            json={"model": model, "messages": [{"role": "user", "content": "hello"}], **extra})

    async def test_restarts_and_partial_capacity_reject_without_accepting_work_then_form_one_cluster(self):
        for available in (0, 1):
            if available:
                self.machine("one")
            response = await self.post(rpc_transport="relay")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["X-Swarm-Status"], "waiting_for_capacity")
            self.assertEqual(response.headers["Retry-After"], "5")
            self.assertFalse(self.c.jobs)
            self.assertFalse(self.c.rpc_clusters)
            self.assertFalse(any(m.in_rpc for m in self.c.machines.values()))
        self.machine("two")
        task = asyncio.create_task(self.post(rpc_transport="relay"))
        try:
            for _ in range(100):
                if self.c.jobs or task.done(): break
                await asyncio.sleep(.005)
            self.assertEqual(len(self.c.jobs), 1)
            self.assertEqual(len(self.c.rpc_clusters), 1)
            job = next(iter(self.c.jobs.values()))
            job.result = {"choices": [{"message": {"content": "Hello"}}]}
            job.done_event.set()
            self.assertEqual((await task).status_code, 200)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_invalid_model_and_context_failures_are_not_marked_as_startup_waits(self):
        for model, extra in (("unknown:model", {}), ("qwen3:30b-a3b", {"options": {"num_ctx": 999999}})):
            response = await self.post(model, **extra)
            self.assertIn(response.status_code, (422, 503))
            self.assertNotIn("X-Swarm-Status", response.headers)

    async def test_queue_saturation_keeps_existing_backpressure_instead_of_startup_retry(self):
        for _ in range(self.c.CHAT_QUEUE_LIMIT_PER_MODEL):
            self.enqueue("qwen3:30b-a3b")
        response = await self.post()
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("X-Swarm-Status", response.headers)
