"""Exercise joins, mixed GPU sizes, simultaneous pollers, and lost workers."""
import asyncio
import io
import time
import threading
import unittest
from unittest.mock import Mock, patch

from test_agent_reports import ROOT, load
from scheduling_support import SchedulingFixture


class SchedulingTests(SchedulingFixture):
    async def test_concurrent_polling_shares_work_and_never_double_books_one_machine(self):
        for name in ("one", "two", "new"):
            self.machine(name)
        for _ in range(6):
            self.enqueue()
        results = await asyncio.gather(*(self.claim(name) for name in ("one", "one", "two", "two", "new", "new")))
        claimed = [r.json()["id"] for r in results if r.status_code == 200]
        self.assertEqual(len(claimed), 3)
        self.assertEqual(len(set(claimed)), 3)
        self.assertEqual(len(self.c._queued_chat_jobs()), 3)

    async def test_all_workload_types_respect_the_shared_busy_slot(self):
        machine = self.machine("gpu")
        image = self.c.ImageJob("sdxl", "test", "", 1, "1024x1024", "standard")
        self.c.image_jobs[image.id] = image
        self.c.image_job_queues["sdxl"] = asyncio.Queue()
        self.c.image_job_queues["sdxl"].put_nowait(image)
        self.enqueue()
        self.assertEqual((await self.claim("gpu")).status_code, 200)
        owner = machine.busy_job_id
        for path in ("/agent/media/jobs/next?agent_id=gpu&vram_gb=999",
                     "/agent/container/jobs/next?agent_id=gpu"):
            self.assertEqual((await self.client.get(path, headers=self.headers)).status_code, 204)
        self.assertEqual(machine.busy_job_id, owner)
        self.assertIsNone(self.c._form_rpc_cluster("qwen3:30b-a3b", 19))
        self.assertEqual(image.status, "queued")

    async def test_duplicate_agent_ids_on_one_physical_host_cannot_double_book_its_gpu(self):
        first, duplicate = self.machine("first"), self.machine("duplicate")
        duplicate.lan_ip = first.lan_ip
        self.enqueue()
        self.enqueue()
        self.assertEqual((await self.claim("first")).status_code, 200)
        self.assertEqual((await self.claim("duplicate")).status_code, 204)
        first.busy, first.busy_job_id = False, None
        self.assertIsNone(self.c._form_rpc_cluster("qwen3:30b-a3b", 19))

    async def test_media_claim_prevents_chat_and_ignores_exaggerated_vram(self):
        machine = self.machine("gpu", 4)
        image = self.c.ImageJob("sdxl", "test", "", 1, "1024x1024", "standard")
        self.c.image_job_queues["sdxl"] = asyncio.Queue()
        self.c.image_job_queues["sdxl"].put_nowait(image)
        path = "/agent/media/jobs/next?agent_id=gpu&vram_gb=999"
        self.assertEqual((await self.client.get(path, headers=self.headers)).status_code, 204)
        machine.gpu_vram_gb = 12
        self.assertEqual((await self.client.get(path, headers=self.headers)).status_code, 200)
        self.enqueue()
        self.assertEqual((await self.claim("gpu")).status_code, 204)

    async def test_new_machine_does_not_mutate_live_cluster_or_steal_rpc_jobs(self):
        for name in ("primary", "worker"):
            self.machine(name)
        cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19, ctx_size=32768, transport="relay")
        self.machine("new")
        rpc_job = self.enqueue("qwen3:30b-a3b", 32768)
        self.assertEqual((await self.claim("new", rpc_job.model)).status_code, 204)
        self.assertEqual((await self.claim("worker", rpc_job.model)).status_code, 204)
        self.assertEqual((await self.claim("primary", rpc_job.model)).status_code, 204)
        cluster.primary_ready, cluster.status = True, "active"
        self.assertEqual((await self.claim("primary", rpc_job.model)).status_code, 200)
        self.enqueue()
        self.assertEqual((await self.claim("new")).status_code, 200)
        self.assertEqual(cluster.worker_ids, ["worker"])

    async def test_new_large_gpu_can_host_next_native_cluster_alone(self):
        self.machine("small", 8)
        self.machine("large", 24)
        cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19, ctx_size=32768)
        self.assertEqual(cluster.primary_id, "large")
        self.assertEqual(cluster.worker_ids, [])
        self.assertFalse(self.c.machines["small"].in_rpc)

    async def test_new_rpc_demand_leaves_capacity_for_an_older_queued_single_gpu_job(self):
        for name in ("one", "two", "three"):
            self.machine(name)
        self.enqueue()
        cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19)
        self.assertIsNotNone(cluster)
        free = [m for m in self.c.machines.values() if not m.in_rpc]
        self.assertEqual(len(free), 1)
        self.assertEqual((await self.claim(free[0].agent_id)).status_code, 200)

    async def test_old_cluster_cleanup_cannot_remove_a_new_cluster_assignment(self):
        for name in ("primary", "worker"):
            self.machine(name)
        old = self.c._form_rpc_cluster("qwen3:30b-a3b", 19)
        old.status, old.dissolved_at = "dissolving", time.time() - 400
        for machine in self.c.machines.values():
            machine.rpc_cluster_id = "new"
            self.c.agent_rpc_assignments[machine.agent_id] = {"cluster_id": "new", "role": "worker"}
        with patch.object(self.c.asyncio, "sleep", side_effect=[None, asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                await self.c._rpc_lifecycle_loop()
        self.assertNotIn(old.cluster_id, self.c.rpc_clusters)
        for machine in self.c.machines.values():
            self.assertTrue(machine.in_rpc)
            self.assertEqual(machine.rpc_cluster_id, "new")
            self.assertEqual(self.c.agent_rpc_assignments[machine.agent_id]["cluster_id"], "new")

    async def test_worker_failure_dissolves_cluster_and_stale_done_is_harmless(self):
        for name in ("primary", "worker"):
            self.machine(name)
        cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19)
        response = await self.client.post("/agent/rpc/done", headers=self.headers,
            json={"agent_id": "worker", "cluster_id": cluster.cluster_id, "role": "worker"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(cluster.status, "dissolving")
        self.assertTrue(self.c.agent_rpc_assignments["primary"]["dissolve"])
        self.c.agent_rpc_assignments["worker"] = {"cluster_id": "new", "role": "worker"}
        response = await self.client.post("/agent/rpc/done", headers=self.headers,
            json={"agent_id": "worker", "cluster_id": cluster.cluster_id, "role": "worker"})
        self.assertTrue(response.json()["stale"])
        self.assertEqual(self.c.agent_rpc_assignments["worker"]["cluster_id"], "new")

    async def test_re_registration_keeps_busy_slot_and_refreshes_port_and_zero_vram(self):
        machine = self.machine("gpu")
        job = self.enqueue()
        token = (await self.claim("gpu")).json()["claim_token"]
        response = await self.client.post("/register", headers=self.headers,
            json={"agent_id": "gpu", "hostname": "gpu", "port": 11435, "model": "qwen3:8b", "gpu_vram_gb": 0})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(machine.busy)
        self.assertEqual(machine.busy_job_id, job.id)
        self.assertEqual(machine.busy_claim_token, token)
        self.assertEqual(machine.port, 11435)
        self.assertEqual(machine.gpu_vram_gb, 0)

    async def test_cloned_live_identity_is_rejected_without_overwriting_machine(self):
        machine = self.machine("original")
        response = await self.client.post("/register", headers=self.headers, json={
            "agent_id": "original", "hostname": "clone", "port": 11434, "model": "qwen3:8b"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(machine.hostname, "original")

    async def test_cancelled_native_job_holds_slot_until_result_acknowledgement(self):
        machine = self.machine("gpu")
        job = self.enqueue()
        token = (await self.claim("gpu")).json()["claim_token"]
        self.c._cancel_job(job.id, "client_disconnected")
        await self.client.post("/heartbeat", headers=self.headers, json={"agent_id": "gpu", "port": 11434,
            "active_job_id": None})
        self.enqueue()
        self.assertTrue(machine.busy)
        self.assertEqual((await self.claim("gpu")).status_code, 204)
        self.assertEqual((await self.submit(job, token)).status_code, 404)
        self.assertFalse(machine.busy)
        self.assertEqual((await self.claim("gpu")).status_code, 200)

    async def test_delayed_idle_heartbeat_cannot_invalidate_new_chat_claim(self):
        machine = self.machine("gpu")
        job = self.enqueue()
        token = (await self.claim("gpu")).json()["claim_token"]
        await self.client.post("/heartbeat", headers=self.headers, json={"agent_id": "gpu", "port": 11434,
            "active_job_id": None})
        self.assertTrue(machine.busy)
        self.assertEqual(job.assigned_agent_id, "gpu")
        self.assertEqual(job.claim_token, token)
        self.assertFalse(self.c._queued_chat_jobs())
        self.assertEqual((await self.submit(job, token)).status_code, 200)

    async def test_delayed_idle_heartbeat_cannot_fail_new_media_claim(self):
        machine = self.machine("gpu")
        job = self.c.ImageJob("sdxl", "test", "", 1, "1024x1024", "standard")
        self.c.image_jobs[job.id] = job
        job.status = "processing"
        machine.busy, machine.busy_job_id = True, job.id
        await self.client.post("/heartbeat", headers=self.headers, json={"agent_id": "gpu", "port": 11434,
            "active_job_id": None})
        self.assertTrue(machine.busy)
        self.assertEqual(job.status, "processing")

    async def test_reclaimed_job_rejects_late_result_without_freeing_new_owner(self):
        old, new = self.machine("old"), self.machine("new")
        job = self.enqueue()
        old_token = (await self.claim("old")).json()["claim_token"]
        self.c._requeue_chat_from(old)
        new_token = (await self.claim("new")).json()["claim_token"]
        self.assertNotEqual(old_token, new_token)
        self.assertEqual((await self.submit(job, old_token, "stale")).status_code, 409)
        self.assertFalse(old.busy)
        self.assertTrue(new.busy)
        self.assertFalse(job.done_event.is_set())
        self.assertEqual((await self.submit(job, new_token, "current")).status_code, 200)
        self.assertEqual(job.result["choices"][0]["message"]["content"], "current")

    async def test_lost_worker_requeues_chat_once_but_does_not_free_its_physical_slot(self):
        old = self.machine("old")
        self.machine("new")
        job = self.enqueue()
        await self.claim("old")
        old.last_heartbeat = time.time() - 130
        with patch.object(self.c.asyncio, "sleep", side_effect=[None, asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                await self.c._health_loop()
        self.assertFalse(old.alive)
        self.assertTrue(old.busy)
        self.assertEqual(len(self.c._queued_chat_jobs()), 1)
        self.assertEqual((await self.claim("new")).json()["id"], job.id)

    async def test_restart_requeues_chat_and_preserves_new_attempt_ownership(self):
        machine = self.machine("gpu")
        machine.runtime = {"agent_pid": 10}
        job = self.enqueue()
        old_token = (await self.claim("gpu")).json()["claim_token"]
        await self.client.post("/register", headers=self.headers, json={"agent_id": "gpu", "hostname": "gpu",
            "agent_pid": 11, "port": 11434, "model": "qwen3:14b"})
        new_token = (await self.claim("gpu")).json()["claim_token"]
        self.assertEqual((await self.submit(job, old_token)).status_code, 409)
        self.assertTrue(machine.busy)
        self.assertEqual((await self.submit(job, new_token)).status_code, 200)

    async def test_busy_recovery_heartbeat_restores_physical_slot_after_coordinator_restart(self):
        machine = self.machine("gpu")
        await self.client.post("/heartbeat", headers=self.headers, json={"agent_id": "gpu", "port": 11434,
            "active_job_id": "old-job", "active_claim_token": "old-token"})
        await self.client.post("/heartbeat", headers=self.headers, json={"agent_id": "gpu", "port": 11434,
            "active_job_id": None})
        self.assertTrue(machine.busy)
        self.enqueue()
        self.assertEqual((await self.claim("gpu")).status_code, 204)
        response = await self.client.post("/agent/jobs/old-job/result", headers=self.headers,
            json={"claim_token": "old-token", "error": "coordinator restarted"})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(machine.busy)

    async def test_context_budget_filters_new_small_gpu_and_preserves_queue(self):
        self.machine("small", 12)
        self.machine("large", 24)
        big = self.enqueue(context=32768)
        small = self.enqueue(context=2048)
        response = await self.claim("small")
        self.assertEqual(response.json()["id"], small.id)
        response = await self.claim("large")
        self.assertEqual(response.json()["id"], big.id)

    async def test_oversized_explicit_context_fails_instead_of_spilling_to_small_gpu(self):
        self.machine("gpu", 12)
        response = await self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"}, json={
            "model": "qwen3:14b", "messages": [{"role": "user", "content": "hello"}], "options": {"num_ctx": 32768}})
        self.assertEqual(response.status_code, 503)
        self.assertIn("8192", response.json()["detail"])
        self.assertFalse(self.c.jobs)

    async def test_container_claim_respects_hardware_and_blocks_other_work(self):
        machine = self.machine("gpu")
        response = await self.client.post("/container/jobs", headers={"X-API-Key": "admin"},
            json={"image": "alpine:3", "cpus": 16, "memory_mb": 65536})
        self.assertEqual(response.status_code, 201)
        path = "/agent/container/jobs/next?agent_id=gpu"
        self.assertEqual((await self.client.get(path, headers=self.headers)).status_code, 204)
        response = await self.client.post("/container/jobs", headers={"X-API-Key": "admin"},
            json={"image": "alpine:3", "cpus": 1, "memory_mb": 1024})
        self.assertEqual((await self.client.get(path, headers=self.headers)).json()["id"], response.json()["id"])
        self.enqueue()
        self.assertTrue(machine.busy)
        self.assertEqual((await self.claim("gpu")).status_code, 204)

    async def test_expired_media_is_not_duplicated_and_late_upload_cannot_revive_it(self):
        machine = self.machine("gpu")
        job = self.c.ImageJob("sdxl", "test", "", 1, "1024x1024", "standard")
        self.c.image_jobs[job.id] = job
        job.status, job.dequeued_at = "processing", time.time() - 700
        machine.busy, machine.busy_job_id, machine.media_phase = True, job.id, "RUNNING"
        with patch.object(self.c.asyncio, "sleep", side_effect=[None, asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                await self.c._media_requeue_loop()
        self.assertEqual(job.status, "failed")
        self.assertTrue(machine.busy)
        self.assertFalse(self.c.image_job_queues)
        response = await self.client.post(f"/agent/media/jobs/{job.id}/result", headers=self.headers, content=b"late-image")
        self.assertTrue(response.json()["stale"])
        self.assertFalse(machine.busy)
        self.assertEqual(job.status, "failed")

    async def test_cpu_and_undersized_gpu_cannot_take_gpu_model(self):
        for name, vram in (("cpu", 0), ("tiny", 4), ("enough", 12)):
            self.machine(name, vram)
        self.enqueue()
        self.assertEqual((await self.claim("cpu")).status_code, 204)
        self.assertEqual((await self.claim("tiny")).status_code, 204)
        self.assertEqual((await self.claim("enough")).status_code, 200)
        self.assertEqual((await self.claim("unregistered")).status_code, 409)

    async def test_queue_discovery_skips_cancelled_entries_and_orders_oldest_demand(self):
        self.machine("gpu")
        cancelled = self.enqueue()
        self.c._cancel_job(cancelled.id, "test")
        self.enqueue("qwen3:8b")
        self.enqueue("qwen3:14b")
        response = await self.client.get("/agent/jobs/queued-models?agent_id=gpu", headers=self.headers)
        self.assertEqual(response.json()["models"], ["qwen3:8b", "qwen3:14b"])
        self.assertEqual((await self.client.get("/machines", headers={"X-API-Key": "admin"})).json()["queued"], 2)

    async def test_overfull_queue_rejects_new_request_with_backpressure(self):
        self.machine("gpu")
        for _ in range(self.c.CHAT_QUEUE_LIMIT_PER_MODEL):
            self.enqueue()
        response = await self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"},
            json={"model": "qwen3:14b", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "5")

    async def test_language_default_and_context_policy_preserve_explicit_system_prompt(self):
        self.machine("gpu")
        for system in (None, "Answer in Spanish."):
            messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": "hello"}]
            task = asyncio.create_task(self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"},
                json={"model": "qwen3:14b", "messages": messages}))
            try:
                for _ in range(100):
                    if self.c.jobs: break
                    await asyncio.sleep(.001)
                job = next(iter(self.c.jobs.values()))
                first = job.body["messages"][0]
                self.assertEqual(first["role"], "system")
                self.assertEqual(first["content"], system) if system else self.assertIn("English", first["content"])
                self.assertEqual(job.body["options"]["num_ctx"], 8192)
                job.result = {"choices": [{"message": {"content": "Hello"}}]}
                job.done_event.set()
                self.assertEqual((await asyncio.wait_for(task, 2)).status_code, 200)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


class AgentSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.a = load("scheduling_agent", ROOT / "agents/agent.py")

    def test_recommended_models_fit_default_hardware_headroom(self):
        expected = {0: "qwen2.5-coder:1.5b", 4: "qwen2.5-coder:1.5b", 8: "qwen3:8b",
                    12: "qwen3:14b", 16: "devstral-small-2:24b", 18: "devstral-small-2:24b", 24: "qwen3:30b-a3b"}
        for vram, model in expected.items():
            with self.subTest(vram=vram):
                self.assertEqual(self.a._MODEL_CATALOG[self.a.recommended_model_index(vram)]["id"], model)

    def test_model_pull_requires_a_successful_claim_and_reports_failure_to_its_owner(self):
        a = self.a
        a._agent_id, a._ollama_port = "gpu", 11434
        events = []
        discovery = 0
        def get(url, **kwargs):
            nonlocal discovery
            if "queued-models" in url:
                discovery += 1
                if discovery > 1: raise KeyboardInterrupt
                return Mock(json=Mock(return_value={"models": ["qwen3:8b"]}))
            events.append("claim")
            return Mock(status_code=200, json=Mock(return_value={"id": "job", "body_json": "{}", "claim_token": "lease"}))
        def pull(*args):
            events.append("pull")
            self.assertEqual(events, ["claim", "pull"])
            raise RuntimeError("disk full")
        with patch.object(a.requests, "get", side_effect=get), patch.object(a.requests, "post") as post, \
             patch.object(a.subprocess, "check_output", return_value="NAME ID SIZE\n"), \
             patch.object(a, "wait_ollama_ready", return_value=True), patch.object(a, "write_status"), \
             patch.object(a, "_check_for_update"), patch.object(a, "_log_crash"), \
             patch.object(a, "pull_model", side_effect=pull), patch.object(a.time, "sleep"):
            with self.assertRaises(KeyboardInterrupt):
                a.work_loop("ollama", "http://coordinator", "key", 11434, 12)
        result = next(call.kwargs["json"] for call in post.call_args_list if "/jobs/job/result" in call.args[0])
        self.assertEqual(result, {"error": "disk full", "claim_token": "lease"})
        self.assertEqual(a._chat_claim, (None, None))

    def test_updates_wait_for_chat_or_media_and_qwen_sampling_preserves_overrides(self):
        a = self.a
        for chat, media in ((("job", "token"), None), ((None, None), "image")):
            a._chat_claim, a._media_job_id = chat, media
            with patch.object(a.requests, "get") as get:
                a._check_for_update("http://coordinator", {})
            get.assert_not_called()
        with patch.object(a.requests, "post", return_value=Mock(json=Mock(return_value={}))) as post:
            a.rpc_infer({"model": "qwen3:30b-a3b", "messages": [], "options": {"temperature": .2}}, 18080)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["temperature"], .2)
        self.assertEqual((body["top_p"], body["top_k"], body["min_p"]), (.95, 20, 0))

    def test_cancellation_does_not_release_the_agent_while_ollama_thread_is_running(self):
        a = self.a
        cancelled, finish, returned = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def post(*args, **kwargs):
            finish.wait(2)
            return Mock(text='{}')
        def get(*args, **kwargs):
            cancelled.set()
            return Mock(status_code=200, json=Mock(return_value={"cancelled": True}))
        def run():
            try:
                a.infer(11434, '{}', 'job', 'http://coordinator', 'key')
            except RuntimeError as exc:
                errors.append(str(exc))
            finally:
                returned.set()
        real_sleep = time.sleep
        with patch.object(a.requests, "post", side_effect=post), patch.object(a.requests, "get", side_effect=get), \
             patch.object(a.time, "sleep", side_effect=lambda _: real_sleep(.001)), \
             patch.object(a.sys, "stdout", io.StringIO()):
            runner = threading.Thread(target=run)
            runner.start()
            try:
                self.assertTrue(cancelled.wait(1))
                self.assertFalse(returned.wait(.05))
            finally:
                finish.set()
                runner.join(2)
        self.assertTrue(returned.is_set())
        self.assertIn("cancelled", errors[0])

    def test_chat_weights_are_explicitly_unloaded_before_other_gpu_work(self):
        a = self.a
        with patch.object(a.requests, "get", return_value=Mock(json=Mock(return_value={"models": [{"name": "qwen3:14b"}]}))), \
             patch.object(a.requests, "post") as post:
            a._unload_ollama_models()
        self.assertEqual(post.call_args.kwargs["json"], {"model": "qwen3:14b", "keep_alive": 0})

    def test_native_rpc_receives_explicit_output_budget(self):
        a = self.a
        with patch.object(a.requests, "post", return_value=Mock(json=Mock(return_value={}))) as post:
            a.rpc_infer({"model": "qwen3:30b-a3b", "messages": [], "options": {"num_predict": 32}}, 18080)
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 32)


if __name__ == "__main__":
    unittest.main()
