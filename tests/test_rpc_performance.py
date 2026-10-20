"""Runtime trials must retain quality settings and never interrupt queued work."""
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch
from scheduling_support import SchedulingFixture
from test_agent_reports import ROOT, load


class PerformanceTests(SchedulingFixture):
    def pair(self, profile=None):
        for name in ("one", "two"):
            self.machine(name).runtime = {"rpc_quic": True, "rpc_tuning": True, "rpc_preflight": True}
        return self.c._form_rpc_cluster("qwen3:30b-a3b", 19, 32768, "quic", runtime=profile)

    async def test_profiles_are_validated_before_any_gpu_reservation(self):
        for value in ({"batch_size": True}, {"batch_size": 999}, {"ubatch_size": 256},
                      {"ctx_size": 2048}, {"cache_ram": -1}):
            response = await self.client.post("/v1/chat/completions", headers={"X-API-Key": "admin"},
                json={"model": "qwen3:30b-a3b", "rpc_transport": "quic", "rpc_agent_ids": ["one", "two"],
                      "rpc_runtime": value, "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(response.status_code, 422)
        self.assertFalse(self.c.rpc_clusters)
        response = await self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"},
            json={"model": "qwen3:30b-a3b", "rpc_runtime": {}, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response.status_code, 403)

    async def test_new_profile_is_in_assignment_and_cannot_silently_reuse_a_different_cluster(self):
        cluster = self.pair({"batch_size": 256, "ubatch_size": 128, "cache_ram": 0})
        assignment = self.c.agent_rpc_assignments[cluster.primary_id]
        self.assertEqual(assignment["ctx_size"], 32768)
        self.assertEqual(assignment["runtime_profile"], cluster.to_dict()["runtime_profile"])
        response = await self.client.post("/v1/chat/completions", headers={"X-API-Key": "admin"}, json={
            "model": cluster.model, "rpc_transport": "quic", "rpc_agent_ids": ["one", "two"],
            "rpc_runtime": {}, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.c.jobs)

    async def test_only_admin_can_stop_and_only_after_queue_and_busy_state_are_empty(self):
        cluster = self.pair(); cluster.status = "active"; cluster.primary_ready = True
        url = "/rpc/clusters/" + cluster.cluster_id + "/stop"
        self.assertEqual((await self.client.post(url, headers={"X-API-Key": "consumer"})).status_code, 401)
        job = self.enqueue(cluster.model); job.rpc_cluster_id = cluster.cluster_id
        self.assertEqual((await self.client.post(url, headers={"X-API-Key": "admin"})).status_code, 409)
        self.c._cancel_job(job.id, "test")
        self.c.machines[cluster.primary_id].busy = True
        self.assertEqual((await self.client.post(url, headers={"X-API-Key": "admin"})).status_code, 409)
        self.c.machines[cluster.primary_id].busy = False
        self.assertEqual((await self.client.post(url, headers={"X-API-Key": "admin"})).status_code, 200)
        self.assertTrue(self.c.machines[cluster.primary_id].in_rpc)
        self.assertTrue(self.c.agent_rpc_assignments[cluster.primary_id]["dissolve"])

    async def test_legacy_primary_cannot_be_assigned_unimplemented_runtime_flags(self):
        for name in ("one", "two"):
            self.machine(name).runtime = {"rpc_quic": True}
        self.assertIsNone(self.c._form_rpc_cluster("qwen3:30b-a3b", 19, 32768, "quic", runtime={"cache_ram": 0}))
        self.assertFalse(any(m.in_rpc for m in self.c.machines.values()))

    async def test_model_specific_defaults_do_not_retune_other_models(self):
        with patch.object(self.c, "RPC_RUNTIME_PROFILES", {"qwen3:30b-a3b": {"cache_ram": 0}}):
            cluster = self.pair()
            self.assertEqual(cluster.runtime_profile["cache_ram"], 0)
            for name in ("three", "four"):
                self.machine(name).runtime = {"rpc_quic": True, "rpc_tuning": True}
            other = self.c._form_rpc_cluster("qwen3:32b", 20, 4096, "quic")
            self.assertEqual(other.runtime_profile["cache_ram"], 8192)


class NativePerformanceTests(unittest.TestCase):
    def test_offload_report_uses_the_current_process_log_only(self):
        agent = load("offload_report_agent", ROOT / "agents/agent.py")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "native.log"
            old = b"offloaded 49/49 layers\n"
            path.write_bytes(old + b"offloaded 48/49 layers\n")
            with patch.object(agent, "_rpc_log_path", path), patch.object(agent, "_rpc_log_start", len(old)):
                self.assertEqual(agent._read_rpc_offload_info(), (48, 49))

    def test_native_runtime_flags_do_not_accept_arbitrary_arguments(self):
        agent = load("performance_agent", ROOT / "agents/agent.py")
        self.assertEqual(agent._rpc_runtime_arguments({"batch_size": 256, "ubatch_size": 128, "cache_ram": 0}),
            ["--batch-size", "256", "--ubatch-size", "128", "--cache-ram", "0"])
        for value in ({"ngl": 2}, {"batch_size": True}, {"ubatch_size": 512}, {"cache_ram": -1}):
            with self.assertRaises(ValueError):
                agent._rpc_runtime_arguments(value)
