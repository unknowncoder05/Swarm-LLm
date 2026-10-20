"""Adversarial admission, cancellation, heartbeat and placement scenarios."""
import asyncio
import json
import threading
import time
import unittest
from unittest.mock import patch

from scheduling_support import SchedulingFixture


class CapacityBoundaryTests(SchedulingFixture):
    async def request(self, **overrides):
        return await self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"}, json={
            "model": "qwen3:14b", "messages": [{"role": "user", "content": "hello"}], **overrides})

    async def start_request(self, **overrides):
        task = asyncio.create_task(self.request(**overrides))
        for _ in range(100):
            if self.c.jobs or task.done():
                break
            await asyncio.sleep(.001)
        self.assertTrue(self.c.jobs, task.result().text if task.done() else "No queued job")
        return task, next(iter(self.c.jobs.values()))

    async def test_cancellation_storm_does_not_accumulate_hidden_queue_entries(self):
        for _ in range(100):
            job = self.enqueue()
            self.c._cancel_job(job.id, "client_disconnected")
        self.assertFalse(self.c.jobs)
        self.assertFalse(self.c._queued_chat_jobs())
        self.assertEqual(self.c.job_queues["qwen3:14b"].qsize(), 0)
        current = self.enqueue()
        self.machine("gpu")
        self.assertEqual((await self.claim("gpu")).json()["id"], current.id)

    async def test_cancelled_http_task_removes_waiter_but_holds_running_gpu(self):
        machine = self.machine("gpu")
        task, job = await self.start_request()
        token = (await self.claim("gpu")).json()["claim_token"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertNotIn(job.id, self.c.jobs)
        self.assertTrue(job.done_event.is_set())
        self.assertTrue(machine.busy)
        self.assertEqual((await self.submit(job, token)).status_code, 404)
        self.assertFalse(machine.busy)

    async def test_cancelled_http_task_removes_unclaimed_queue_entry(self):
        self.machine("gpu")
        task, job = await self.start_request()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertNotIn(job.id, self.c.jobs)
        self.assertEqual(self.c.job_queues[job.model].qsize(), 0)
        self.assertEqual((await self.claim("gpu")).status_code, 204)

    async def test_completed_chat_cannot_be_resurrected_by_delayed_active_heartbeat(self):
        machine = self.machine("gpu")
        job = self.enqueue()
        token = (await self.claim("gpu")).json()["claim_token"]
        self.assertEqual((await self.submit(job, token)).status_code, 200)
        self.c.jobs.pop(job.id)
        response = await self.client.post("/heartbeat", headers=self.headers, json={
            "agent_id": "gpu", "port": 11434, "active_job_id": job.id, "active_claim_token": token})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(machine.busy)
        self.enqueue()
        self.assertEqual((await self.claim("gpu")).status_code, 200)

    async def test_completed_media_cannot_be_resurrected_by_delayed_active_heartbeat(self):
        machine = self.machine("gpu")
        machine.busy, machine.busy_job_id = True, "old-image"
        self.c._release_media_slot("old-image")
        await self.client.post("/heartbeat", headers=self.headers, json={
            "agent_id": "gpu", "port": 11434, "active_job_id": "old-image"})
        self.assertFalse(machine.busy)

    async def test_large_new_gpu_avoids_regional_relay_even_without_external_capability(self):
        large = self.machine("large", 24)
        self.c.RPC_RELAY_URL = "https://regional"
        self.c.RPC_RELAY_EXPIRES_AT = time.time() + 3600
        cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19, ctx_size=32768, transport="relay")
        self.assertIsNotNone(cluster)
        self.assertEqual(cluster.primary_id, large.agent_id)
        self.assertEqual(cluster.worker_ids, [])
        self.assertIsNone(cluster.relay_url)
        self.assertTrue(cluster.relay_ready)

    async def test_full_global_queue_does_not_reserve_gpus_or_start_regional_setup(self):
        for name in ("one", "two"):
            self.machine(name).runtime = {"rpc_relay_external": True}
        self.c.RPC_RELAY_URL = "https://regional"
        for index in range(self.c.CHAT_QUEUE_LIMIT_TOTAL):
            self.enqueue(f"custom-{index}")
        with patch.object(self.c, "_regional_relay_call", return_value={}) as control:
            response = await self.request(model="qwen3:30b-a3b")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "5")
        self.assertFalse(self.c.rpc_clusters)
        self.assertFalse(any(m.in_rpc for m in self.c.machines.values()))
        control.assert_not_called()

    async def test_oversized_chat_body_is_rejected_before_gpu_reservation(self):
        self.machine("large", 24)
        response = await asyncio.wait_for(self.request(model="qwen3:30b-a3b", messages=[
            {"role": "user", "content": "a" * (2 * 1024 * 1024)}]), .5)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(self.c.jobs)
        self.assertFalse(self.c.rpc_clusters)

    async def test_streamed_body_cannot_bypass_size_bound_with_missing_or_false_length(self):
        async def chunks():
            for _ in range(33):
                yield b"x" * 65536
        for headers in ({}, {"Content-Length": "2"}):
            response = await self.client.post("/v1/chat/completions", content=chunks(),
                headers={"X-API-Key": "consumer", **headers})
            self.assertEqual(response.status_code, 413)
        self.assertFalse(self.c.jobs)

    async def test_model_and_global_byte_budgets_reject_before_model_formation(self):
        for scope in ("model", "global"):
            with self.subTest(scope=scope):
                job = self.enqueue("qwen3:30b-a3b" if scope == "model" else "custom-model")
                with patch.object(self.c, "CHAT_QUEUE_BYTES_PER_MODEL", 1 if scope == "model" else 10**9, create=True), \
                     patch.object(self.c, "CHAT_QUEUE_BYTES_TOTAL", 1 if scope == "global" else 10**9, create=True), \
                     patch.object(self.c, "_form_rpc_cluster") as form:
                    response = await asyncio.wait_for(self.request(model="qwen3:30b-a3b"), .5)
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.headers["retry-after"], "5")
                form.assert_not_called()
                self.c._cancel_job(job.id, "test_complete")

    async def test_cancellation_returns_queued_byte_budget_to_next_request(self):
        self.machine("gpu")
        task, job = await self.start_request()
        # Allow exactly one request, then prove a cancelled waiter returns its budget.
        size = len(json.dumps(job.body).encode())
        with patch.object(self.c, "CHAT_QUEUE_BYTES_PER_MODEL", size + 100, create=True):
            self.assertEqual((await asyncio.wait_for(self.request(), .5)).status_code, 429)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            next_task, next_job = await self.start_request()
            try:
                token = (await self.claim("gpu")).json()["claim_token"]
                await self.submit(next_job, token)
                self.assertEqual((await asyncio.wait_for(next_task, 2)).status_code, 200)
            finally:
                if not next_task.done():
                    next_task.cancel()
                    await asyncio.gather(next_task, return_exceptions=True)

    async def test_unicode_expansion_counts_against_queued_wire_byte_budget(self):
        self.machine("gpu")
        messages = [{"role": "user", "content": "界" * 1000}]
        with patch.object(self.c, "CHAT_QUEUE_BYTES_PER_MODEL", 10000):
            task, job = await self.start_request(messages=messages)
            try:
                response = await asyncio.wait_for(self.request(messages=messages), .5)
                self.assertEqual(response.status_code, 429)
                self.assertEqual(len(self.c.jobs), 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_rpc_primary_does_not_claim_when_either_member_heartbeat_is_stale(self):
        for name in ("primary", "worker"):
            self.machine(name)
        cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19, transport="relay")
        cluster.status, cluster.primary_ready = "active", True
        job = self.enqueue(cluster.model, 32768)
        for name in (cluster.primary_id, *cluster.worker_ids):
            machine = self.c.machines[name]
            machine.last_heartbeat = time.time() - self.c.DEAD_AFTER_SEC - 1
            self.assertEqual((await self.claim(cluster.primary_id, cluster.model)).status_code, 204)
            self.assertIsNone(job.assigned_agent_id)
            machine.last_heartbeat = time.time()
        self.assertEqual((await self.claim(cluster.primary_id, cluster.model)).status_code, 200)

    async def test_cancelled_regional_setup_does_not_strand_gpu_reservations(self):
        for name in ("one", "two"):
            self.machine(name).runtime = {"rpc_relay_external": True}
        self.c.RPC_RELAY_URL = "https://regional"
        started, finish = threading.Event(), threading.Event()
        def setup(*args):
            started.set()
            finish.wait(2)
            return {}
        with patch.object(self.c, "_regional_relay_call", side_effect=setup):
            task = asyncio.create_task(self.request(model="qwen3:30b-a3b", rpc_transport="relay"))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(.001)
                self.assertTrue(started.is_set())
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                cluster = next(iter(self.c.rpc_clusters.values()))
                self.assertEqual(cluster.status, "dissolving")
                self.assertFalse(self.c.jobs)
                self.assertTrue(all(a["dissolve"] for a in self.c.agent_rpc_assignments.values()))
            finally:
                finish.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_rpc_teardown_acknowledgement_releases_aborted_primary_job(self):
        for name in ("primary", "worker"):
            self.machine(name)
        cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19, transport="relay")
        cluster.status, cluster.primary_ready = "active", True
        job = self.enqueue(cluster.model, 32768)
        await self.claim(cluster.primary_id, cluster.model)
        machine = self.c.machines[cluster.primary_id]
        response = await self.client.post("/agent/rpc/done", headers=self.headers, json={
            "agent_id": cluster.primary_id, "cluster_id": cluster.cluster_id, "role": "primary"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(job.done_event.is_set())
        self.assertFalse(machine.busy)
        self.assertFalse(machine.in_rpc)
        self.assertIsNone(machine.busy_job_id)

    async def test_concurrent_admissions_cannot_overfill_queue_during_regional_setup(self):
        for name in ("one", "two"):
            self.machine(name).runtime = {"rpc_relay_external": True}
        self.c.RPC_RELAY_URL = "https://regional"
        started, finish = threading.Event(), threading.Event()
        def setup(*args):
            started.set()
            finish.wait(3)
            return {}
        tasks = []
        with patch.object(self.c, "_regional_relay_call", side_effect=setup):
            first = asyncio.create_task(self.request(model="qwen3:30b-a3b", rpc_transport="relay"))
            tasks.append(first)
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(.001)
                self.assertTrue(started.is_set())
                others = [asyncio.create_task(self.request(model="qwen3:30b-a3b"))
                          for _ in range(self.c.CHAT_QUEUE_LIMIT_PER_MODEL)]
                tasks.extend(others)
                for _ in range(100):
                    if len(self.c.jobs) == self.c.CHAT_QUEUE_LIMIT_PER_MODEL:
                        break
                    await asyncio.sleep(.001)
                self.assertEqual(len(self.c.jobs), self.c.CHAT_QUEUE_LIMIT_PER_MODEL)
                finish.set()
                self.assertEqual((await asyncio.wait_for(first, 2)).status_code, 429)
                self.assertEqual(len(self.c.rpc_clusters), 1)
                self.assertEqual(len(self.c.jobs), self.c.CHAT_QUEUE_LIMIT_PER_MODEL)
                for job in list(self.c.jobs.values()):
                    job.result = {"choices": [{"message": {"content": "OK"}}]}
                    job.done_event.set()
                self.assertTrue(all(response.status_code == 200 for response in await asyncio.gather(*others)))
            finally:
                finish.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_structured_user_content_and_tool_arguments_survive_admission(self):
        self.machine("gpu")
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [{"type": "function", "id": "call-1",
                "function": {"name": "read", "arguments": '{"path":"a.py"}'}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "file contents"},
            {"role": "user", "content": [{"type": "text", "text": "summarize"}]},
        ]
        task, job = await self.start_request(messages=messages)
        try:
            self.assertEqual(job.body["messages"][-1]["content"], messages[-1]["content"])
            self.assertEqual(job.body["messages"][1]["tool_calls"][0]["function"]["arguments"], {"path": "a.py"})
            token = (await self.claim("gpu")).json()["claim_token"]
            await self.submit(job, token)
            self.assertEqual((await asyncio.wait_for(task, 2)).status_code, 200)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_delayed_regional_assignment_response_cannot_reissue_replaced_assignment(self):
        for name in ("primary", "worker"):
            self.machine(name).runtime = {"rpc_relay_external": True}
        self.c.RPC_RELAY_URL = "https://regional"
        cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19, transport="relay")
        cluster.relay_ready = True
        worker = cluster.worker_ids[0]
        def changed_assignment(*args):
            self.c.agent_rpc_assignments[worker] = {"role": "worker", "cluster_id": "replacement"}
            return {"connections": {worker: ["obsolete-channel"]}}
        with patch.object(self.c, "_regional_relay_call", side_effect=changed_assignment):
            response = await self.client.get("/agent/rpc/assignment", headers=self.headers, params={"agent_id": worker})
        self.assertEqual(response.status_code, 204)

    async def test_explicit_output_limits_reach_worker_and_default_remains_unlimited(self):
        self.machine("gpu")
        cases = (({}, -1), ({"max_tokens": 17}, 17), ({"max_completion_tokens": 23}, 23),
                 ({"options": {"num_predict": 31}}, 31),
                 ({"max_tokens": 64, "max_completion_tokens": 32, "options": {"num_predict": 16}}, 16))
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                task, job = await self.start_request(**overrides)
                try:
                    self.assertEqual(job.body["options"]["num_predict"], expected)
                    token = (await self.claim("gpu")).json()["claim_token"]
                    await self.submit(job, token)
                    self.assertEqual((await asyncio.wait_for(task, 2)).status_code, 200)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_invalid_limits_reject_before_cluster_formation(self):
        self.machine("large", 24)
        for field in ("max_tokens", "max_completion_tokens"):
            for value in (0, -1, True, 1.5, "20", None):
                with self.subTest(field=field, value=value):
                    response = await asyncio.wait_for(self.request(model="qwen3:30b-a3b", **{field: value}), .5)
                    self.assertEqual(response.status_code, 422)
                    self.assertFalse(self.c.rpc_clusters)
                    self.assertFalse(self.c.jobs)

    async def test_malformed_request_shapes_never_reserve_gpu_or_raise_server_errors(self):
        self.machine("large", 24)
        bodies = ([], None, 42, {"model": []}, {"model": 42}, {"messages": None},
                  {"messages": "hello"}, {"messages": [None]}, {"messages": [{"role": "user", "content": 42}]},
                  {"messages": [{"role": "assistant", "tool_calls": [None]}]},
                  {"messages": [{"role": "assistant", "tool_calls": [{"function": None}]}]})
        for shape in bodies:
            body = {"model": "qwen3:30b-a3b", "messages": [{"role": "user", "content": "hello"}], **shape} if isinstance(shape, dict) else shape
            with self.subTest(body=body):
                try:
                    response = await asyncio.wait_for(self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"}, json=body), .5)
                except Exception as exc:
                    self.fail(f"Malformed body raised {type(exc).__name__}: {exc}")
                self.assertIn(response.status_code, (400, 422))
                self.assertFalse(self.c.rpc_clusters)
                self.assertFalse(self.c.jobs)


if __name__ == "__main__":
    unittest.main()
