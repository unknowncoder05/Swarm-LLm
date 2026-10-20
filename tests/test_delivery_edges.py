"""Fault injection at claim delivery, result acknowledgment and process boundaries."""
import asyncio
import threading
import time
import unittest
from unittest.mock import Mock, patch

import requests
from scheduling_support import SchedulingFixture
from test_agent_reports import ROOT, load


class DeliveryEdgeTests(SchedulingFixture):
    async def poll(self, name="gpu", request_id="offer-1", model="qwen3:14b"):
        return await self.client.get("/agent/jobs/next", headers=self.headers,
            params={"agent_id": name, "model": model, "claim_request_id": request_id})

    async def test_lost_claim_response_can_be_retried_without_a_second_execution_attempt(self):
        machine = self.machine("gpu")
        job, other = self.enqueue(), self.enqueue()
        original = await self.poll()
        retry = await self.poll()
        self.assertEqual(original.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json(), original.json())
        self.assertEqual(job.attempts, 1)
        self.assertEqual(machine.busy_job_id, job.id)
        self.assertIsNone(other.assigned_agent_id)
        self.assertEqual((await self.poll(request_id="different-poll")).status_code, 204)

    async def test_cancelled_lost_offer_returns_terminal_receipt_instead_of_running_work(self):
        machine = self.machine("gpu")
        job = self.enqueue()
        original = (await self.poll()).json()
        self.c._cancel_job(job.id, "client_disconnected")
        response = await self.poll()
        self.assertEqual(response.status_code, 200)
        receipt = response.json()
        self.assertTrue(receipt["cancelled"])
        self.assertEqual(receipt["claim_token"], original["claim_token"])
        self.assertTrue(machine.busy)
        self.assertEqual((await self.submit(job, receipt["claim_token"])).status_code, 404)
        self.assertFalse(machine.busy)

    async def test_requeued_offer_receipt_cannot_complete_or_release_the_new_attempt(self):
        old, new = self.machine("gpu"), self.machine("new")
        job = self.enqueue()
        token = (await self.poll()).json()["claim_token"]
        self.c._requeue_chat_from(old)
        new_token = (await self.poll("new", "offer-2")).json()["claim_token"]
        receipt = await self.poll()
        self.assertEqual(receipt.status_code, 200)
        self.assertTrue(receipt.json()["cancelled"])
        self.assertEqual((await self.submit(job, token)).status_code, 409)
        self.assertFalse(old.busy)
        self.assertTrue(new.busy)
        self.assertEqual(job.claim_token, new_token)

    async def test_hardware_downgrade_cancels_undelivered_offer(self):
        machine = self.machine("gpu")
        self.enqueue()
        await self.poll()
        machine.gpu_vram_gb = 0
        response = await self.poll()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["cancelled"])

    async def test_claim_request_ids_are_bounded(self):
        self.machine("gpu")
        self.enqueue()
        for invalid in ("", "x" * 65):
            self.assertEqual((await self.poll(request_id=invalid)).status_code, 422)
        self.assertFalse(self.c.machines["gpu"].busy)

    async def test_unknown_result_before_recovery_heartbeat_cannot_resurrect_finished_work(self):
        machine = self.machine("gpu")
        response = await self.client.post("/agent/jobs/pre-restart-job/result", headers=self.headers,
            json={"claim_token": "old-token", "error": "coordinator restarted"})
        self.assertEqual(response.status_code, 404)
        response = await self.client.post("/heartbeat", headers=self.headers, json={
            "agent_id": "gpu", "port": 11434, "active_job_id": "pre-restart-job", "active_claim_token": "old-token"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(machine.busy)
        # A different attempt remains protected; an old acknowledgment cannot
        # declare that newer computation finished.
        await self.client.post("/heartbeat", headers=self.headers, json={
            "agent_id": "gpu", "port": 11434, "active_job_id": "pre-restart-job", "active_claim_token": "new-token"})
        self.assertTrue(machine.busy)

    async def test_actual_agent_recovers_dropped_claim_response_and_dropped_result_ack(self):
        machine = self.machine("gpu")
        job = self.enqueue()
        agent = load("real_delivery_agent", ROOT / "agents/agent.py")
        agent._agent_id = "gpu"
        loop = asyncio.get_running_loop()
        claim_calls, result_calls = [], []
        def get(url, **kwargs):
            response = asyncio.run_coroutine_threadsafe(self.client.get("/agent/jobs/next",
                params=kwargs["params"], headers=self.headers), loop).result(timeout=2)
            claim_calls.append(response)
            if len(claim_calls) == 1:
                raise requests.Timeout("Claim accepted, reply lost")
            return response
        def post(url, **kwargs):
            if not result_calls:
                result_calls.append(503)
                return Mock(status_code=503)
            response = asyncio.run_coroutine_threadsafe(self.client.post(f"/agent/jobs/{job.id}/result",
                json=kwargs["json"], headers=self.headers), loop).result(timeout=2)
            result_calls.append(response.status_code)
            if len(result_calls) == 2:
                raise requests.Timeout("Result committed, acknowledgment lost")
            return response
        with patch.object(agent.requests, "get", side_effect=get), patch.object(agent.requests, "post", side_effect=post), \
             patch.object(agent.time, "sleep"), patch.object(agent, "_log_crash"), patch.object(agent, "err"):
            with self.assertRaises(requests.Timeout):
                await asyncio.to_thread(agent._claim_chat_job, "https://coordinator", "key", job.model)
            offer = await asyncio.to_thread(agent._claim_chat_job, "https://coordinator", "key", job.model)
            self.assertEqual(offer["id"], job.id)
            self.assertEqual(job.attempts, 1)
            agent._chat_claim = (job.id, offer["claim_token"])
            expected = {"choices": [{"message": {"content": "exactly one result"}}]}
            await asyncio.to_thread(agent._post_chat_result, "https://coordinator", "key", job.id,
                payload={"claim_token": offer["claim_token"], "result": expected})
        self.assertEqual(result_calls, [503, 200, 200])
        self.assertEqual(job.result, expected)
        self.assertTrue(job.done_event.is_set())
        self.assertFalse(machine.busy)

    async def test_rpc_offer_retry_does_not_start_after_worker_goes_stale(self):
        for name in ("primary", "worker"):
            self.machine(name)
        cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19, transport="relay")
        cluster.status, cluster.primary_ready = "active", True
        job = self.enqueue(cluster.model, 32768)
        original = await self.poll(cluster.primary_id, model=cluster.model)
        self.assertEqual(original.status_code, 200)
        self.c.machines[cluster.worker_ids[0]].last_heartbeat = time.time() - 130
        receipt = await self.poll(cluster.primary_id, model=cluster.model)
        self.assertEqual(receipt.status_code, 200)
        self.assertTrue(receipt.json()["cancelled"])
        self.assertEqual(job.attempts, 1)

    async def test_restart_heartbeat_cannot_erase_previous_process_identity_before_registration(self):
        machine = self.machine("gpu")
        machine.runtime = {"agent_pid": 101}
        job = self.enqueue()
        await self.claim("gpu")
        response = await self.client.post("/heartbeat", headers=self.headers, json={
            "agent_id": "gpu", "port": 11434, "agent_pid": 202, "active_job_id": None})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(machine.runtime["agent_pid"], 101)
        response = await self.client.post("/register", headers=self.headers, json={
            "agent_id": "gpu", "hostname": "gpu", "port": 11434, "model": "qwen3:14b", "agent_pid": 202})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(machine.busy)
        self.assertIsNone(job.assigned_agent_id)

    async def test_replayed_crash_report_is_archived_without_disabling_healthy_worker(self):
        machine = self.machine("gpu")
        machine.runtime = {"agent_pid": 202}
        response = await self.client.post("/agent-status", headers=self.headers, json={
            "agent_id": "gpu", "report_id": "old-crash", "phase": "ERROR", "detail": "old process failed",
            "diagnostics": {"agent_pid": 101}, "occurred_at": time.time() - 3600})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(machine.phase, "RUNNING")
        reports = (await self.client.get("/agent-reports", headers={"X-API-Key": "admin"})).json()["reports"]
        self.assertEqual(reports[0]["report_id"], "old-crash")
        self.enqueue()
        self.assertEqual((await self.claim("gpu")).status_code, 200)

    async def test_cluster_teardown_during_regional_setup_does_not_admit_stranded_request(self):
        for name in ("primary", "worker"):
            self.machine(name).runtime = {"rpc_relay_external": True}
        self.c.RPC_RELAY_URL = "https://regional"
        entered, finish = threading.Event(), threading.Event()
        def setup(*args):
            entered.set()
            finish.wait(3)
            return {}
        with patch.object(self.c, "_regional_relay_call", side_effect=setup):
            task = asyncio.create_task(self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"}, json={
                "model": "qwen3:30b-a3b", "rpc_transport": "relay", "messages": [{"role": "user", "content": "hi"}]}))
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.001)
                self.assertTrue(entered.is_set())
                cluster = next(iter(self.c.rpc_clusters.values()))
                self.c._dissolve_cluster(cluster, "member_offline")
                finish.set()
                response = await asyncio.wait_for(task, .5)
                self.assertEqual(response.status_code, 503)
                self.assertFalse(self.c.jobs)
            finally:
                finish.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_late_queue_rejection_dissolves_cluster_without_other_accepted_work(self):
        for name in ("primary", "worker"):
            self.machine(name).runtime = {"rpc_relay_external": True}
        self.c.RPC_RELAY_URL = "https://regional"
        def other_admission(*args):
            self.enqueue("custom-model")
            return {}
        with patch.object(self.c, "CHAT_QUEUE_LIMIT_TOTAL", 1), \
             patch.object(self.c, "_regional_relay_call", side_effect=other_admission):
            response = await self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"}, json={
                "model": "qwen3:30b-a3b", "rpc_transport": "relay", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response.status_code, 429)
        cluster = next(iter(self.c.rpc_clusters.values()))
        self.assertEqual(cluster.status, "dissolving")
        self.assertTrue(all(assignment["dissolve"] for assignment in self.c.agent_rpc_assignments.values()))
        self.assertTrue(all(machine.in_rpc for machine in self.c.machines.values()))
        self.assertEqual([job.model for job in self.c.jobs.values()], ["custom-model"])


class AgentDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.a = load("delivery_agent", ROOT / "agents/agent.py")
        self.a._agent_id = "gpu"

    def response(self, status=200, body=None):
        response = Mock(status_code=status)
        response.json.return_value = body or {"id": "job", "body_json": "{}", "claim_token": "token"}
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(str(status), response=response)
        return response

    def test_lost_claim_response_reuses_poll_id_until_a_definite_reply(self):
        a = self.a
        with patch.object(a.requests, "get", side_effect=[requests.Timeout("lost reply"), self.response(), self.response(204)]) as get:
            with self.assertRaises(requests.Timeout):
                a._claim_chat_job("https://coordinator", "key", "qwen3:14b")
            self.assertEqual(a._pending_chat_models("https://coordinator"), ["qwen3:14b"])
            job = a._claim_chat_job("https://coordinator", "key", "qwen3:14b")
            self.assertEqual(job["id"], "job")
            a._claim_chat_job("https://coordinator", "key", "qwen3:14b")
        ids = [call.kwargs["params"]["claim_request_id"] for call in get.call_args_list]
        self.assertEqual(ids[0], ids[1])
        self.assertNotEqual(ids[1], ids[2])

    def test_cancelled_receipt_is_acknowledged_without_inference(self):
        a = self.a
        body = {"id": "cancelled-job", "body_json": "{}", "claim_token": "token", "cancelled": True}
        with patch.object(a.requests, "get", return_value=self.response(body=body)), \
             patch.object(a.requests, "post", return_value=self.response(404)) as post:
            self.assertIsNone(a._claim_chat_job("https://coordinator", "key", "qwen3:14b"))
        self.assertIn("cancelled-job/result", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["json"]["claim_token"], "token")
        self.assertEqual(a._chat_claim, (None, None))

    def test_result_delivery_retries_server_error_and_timeout_before_clearing_claim(self):
        a = self.a
        a._chat_claim = ("job", "token")
        states = []
        def sleep(_): states.append(a._chat_claim)
        with patch.object(a.requests, "post", side_effect=[self.response(503), requests.Timeout("lost ack"), self.response()]) as post, \
             patch.object(a.time, "sleep", side_effect=sleep), patch.object(a, "_log_crash"), patch.object(a, "err"):
            a._post_chat_result("https://coordinator", "key", "job", payload={"claim_token": "token", "error": "example"})
        self.assertEqual(post.call_count, 3)
        self.assertTrue(all(state == ("job", "token") for state in states))

    def test_result_delivery_treats_stale_or_cancelled_claim_as_terminal(self):
        for status in (200, 404, 409):
            with self.subTest(status=status), patch.object(self.a.requests, "post", return_value=self.response(status)) as post:
                self.a._post_chat_result("https://coordinator", "key", "job", payload={"claim_token": "token"})
                self.assertEqual(post.call_count, 1)

    def test_result_delivery_surfaces_permanent_auth_failure(self):
        with patch.object(self.a.requests, "post", return_value=self.response(401)) as post, \
             self.assertRaises(requests.HTTPError):
            self.a._post_chat_result("https://coordinator", "key", "job", payload={"claim_token": "token"})
        self.assertEqual(post.call_count, 1)

    def test_main_loop_retries_pending_claim_when_discovery_returns_no_models(self):
        a = self.a
        a._ollama_port = 11434
        discoveries, claims, ids = [], [], []
        def get(url, **kwargs):
            if "queued-models" in url:
                discoveries.append(True)
                if len(discoveries) > 2:
                    raise KeyboardInterrupt
                return self.response(body={"models": ["qwen3:14b"] if len(discoveries) == 1 else []})
            if "/jobs/next" in url:
                claims.append(True)
                ids.append(kwargs["params"]["claim_request_id"])
                if len(claims) == 1:
                    raise requests.Timeout("lost claim response")
            return self.response()
        with patch.object(a.requests, "get", side_effect=get), patch.object(a.requests, "post", return_value=self.response()), \
             patch.object(a.subprocess, "check_output", return_value="NAME ID SIZE\nqwen3:14b hash 9GB\n"), \
             patch.object(a, "wait_ollama_ready", return_value=True), patch.object(a, "write_status"), \
             patch.object(a, "_check_for_update"), patch.object(a, "_log_crash"), patch.object(a.time, "sleep"), \
             patch.object(a, "infer", return_value='{"choices":[{"message":{"content":"OK"}}]}') as infer:
            with self.assertRaises(KeyboardInterrupt):
                a.work_loop("ollama", "https://coordinator", "key", 11434, 12)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(infer.call_count, 1)
        self.assertEqual(a._chat_claim, (None, None))

    def test_rpc_readiness_retries_transient_delivery_failure(self):
        a = self.a
        proc = Mock(poll=Mock(return_value=None))
        with patch.object(a, "_rpc_poll_assignment", return_value={"cluster_id": "cluster"}), \
             patch.object(a.requests, "post", side_effect=[self.response(503), requests.Timeout(), self.response()]) as post, \
             patch.object(a.time, "sleep"):
            self.assertTrue(a._confirm_rpc_ready("https://coordinator", "key", "cluster", proc))
        self.assertEqual(post.call_count, 3)

    def test_rpc_readiness_aborts_when_assignment_is_replaced_during_retry(self):
        a = self.a
        with patch.object(a, "_rpc_poll_assignment", side_effect=[{"cluster_id": "old"}, {"cluster_id": "new"}]), \
             patch.object(a.requests, "post", return_value=self.response(503)) as post, patch.object(a.time, "sleep"):
            self.assertFalse(a._confirm_rpc_ready("https://coordinator", "key", "old", Mock(poll=Mock(return_value=None))))
        self.assertEqual(post.call_count, 1)

    def test_rpc_readiness_does_not_keep_dead_process_or_retry_forever(self):
        a = self.a
        with self.assertRaisesRegex(RuntimeError, "exited before readiness"):
            a._confirm_rpc_ready("https://coordinator", "key", "cluster", Mock(poll=Mock(return_value=1), returncode=1))
        with patch.object(a, "_rpc_poll_assignment", return_value={"cluster_id": "cluster"}), \
             patch.object(a.requests, "post", return_value=self.response(503)), \
             patch.object(a.time, "monotonic", side_effect=[0, 121]), patch.object(a.time, "sleep"), \
             self.assertRaisesRegex(RuntimeError, "120 seconds"):
            a._confirm_rpc_ready("https://coordinator", "key", "cluster", Mock(poll=Mock(return_value=None)))

    def test_rpc_role_owner_stops_native_process_before_single_teardown_receipt(self):
        a = self.a
        events = []
        def receipt(*args, **kwargs):
            self.assertTrue(a._rpc_active.is_set())
            events.append("receipt")
        with patch.object(a, "_rpc_poll_assignment", side_effect=[{"cluster_id": "cluster", "role": "worker"}, KeyboardInterrupt]), \
             patch.object(a, "run_rpc_worker", side_effect=RuntimeError("native failed")), \
             patch.object(a, "_stop_rpc_relays"), patch.object(a, "_stop_rpc_process", side_effect=lambda: events.append("stop")), \
             patch.object(a, "_post_agent_receipt", side_effect=receipt) as post, \
             patch.object(a, "_restart_ollama_after_rpc", side_effect=lambda: events.append("ollama")), \
             patch.object(a, "_log_crash"), patch.object(a.time, "sleep"):
            with self.assertRaises(KeyboardInterrupt):
                a._rpc_thread_func("https://coordinator", "key")
        self.assertEqual(events, ["stop", "receipt", "ollama"])
        self.assertEqual(post.call_count, 1)
        self.assertFalse(a._rpc_active.is_set())


if __name__ == "__main__":
    unittest.main()
