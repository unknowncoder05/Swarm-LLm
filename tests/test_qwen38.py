"""Qwen3.8 resolves to an explicit artifact without weakening GPU admission."""
import asyncio
from unittest.mock import Mock, patch
from scheduling_support import SchedulingFixture
from test_agent_reports import ROOT, load
import unittest

MODEL = "qwen3.8:27b-q4_K_M"


class Qwen38AdmissionTests(SchedulingFixture):
    async def test_upstream_id_forms_one_native_pair_with_32k_context_and_reasoning(self):
        for name in ("primary", "worker"):
            self.machine(name).runtime = {"rpc_quic": True, "rpc_preflight": True, "rpc_tuning": True}
        task = asyncio.create_task(self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"},
            json={"model": "Qwen/Qwen3.8-27B", "rpc_transport": "quic",
                  "messages": [{"role": "user", "content": "hello"}]}))
        try:
            for _ in range(100):
                if self.c.jobs or task.done(): break
                await asyncio.sleep(.001)
            self.assertTrue(self.c.jobs, "Expected an admitted Qwen3.8 request")
            job = next(iter(self.c.jobs.values()))
            cluster = next(iter(self.c.rpc_clusters.values()))
            self.assertEqual(job.model, MODEL)
            self.assertEqual(cluster.model, MODEL)
            self.assertEqual(cluster.ctx_size, 32768)
            self.assertEqual(len(cluster.worker_ids), 1)
            self.assertNotIn("think", job.body["options"])
            self.assertEqual(job.body["options"]["num_ctx"], 32768)
            job.result = {"choices": [{"message": {"content": "hello"}}]}
            job.done_event.set()
            self.assertEqual((await asyncio.wait_for(task, 2)).status_code, 200)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_one_twelve_gb_worker_cannot_accept_the_dense_model(self):
        self.machine("gpu")
        response = await self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"},
            json={"model": "qwen3.8:27b", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["x-swarm-status"], "waiting_for_capacity")
        self.assertFalse(self.c.jobs)
        self.assertFalse(self.c.rpc_clusters)


class Qwen38WorkerTests(unittest.TestCase):
    def test_model_sampling_defaults_and_explicit_overrides_reach_native_runtime(self):
        agent = load("qwen38_agent", ROOT / "agents/agent.py")
        with patch.object(agent.requests, "post", return_value=Mock(json=Mock(return_value={}))) as post:
            agent.rpc_infer({"model": MODEL, "messages": [], "options": {}}, 18080)
            body = post.call_args.kwargs["json"]
            self.assertEqual(body["temperature"], 1.0)
            self.assertEqual(body["top_p"], .95)
            self.assertEqual(body["top_k"], 20)
            self.assertEqual(body["repeat_penalty"], 1.0)
            agent.rpc_infer({"model": MODEL, "messages": [], "options": {"temperature": .2}}, 18080)
            self.assertEqual(post.call_args.kwargs["json"]["temperature"], .2)

    def test_small_worker_defaults_and_opencode_context_remain_safe(self):
        agent = load("qwen38_agent_defaults", ROOT / "agents/agent.py")
        self.assertEqual(agent._MODEL_CATALOG[agent.recommended_model_index(0)]["id"], "qwen2.5-coder:1.5b")
        self.assertNotEqual(agent._MODEL_CATALOG[agent.recommended_model_index(12)]["id"], MODEL)
        config = load("qwen38_client", ROOT / "scripts/gen_opencode_config.py").build_config(MODEL)
        profile = config["provider"]["swarm-llm"]["models"][MODEL]
        self.assertTrue(profile["reasoning"])
        self.assertTrue(profile["tool_call"])
        self.assertEqual(profile["options"]["options"]["num_ctx"], profile["limit"]["context"])
