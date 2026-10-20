"""QUIC membership, endpoint immutability, capability and secret boundaries."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from test_agent_reports import ROOT, load


class QuicControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"ADMIN_KEY": "admin", "AGENT_KEY": "agent", "CONSUMER_KEY": "consumer",
                                     "RPC_RELAY_URL": "", "RPC_RELAY_ADMIN_KEY": ""}):
            self.c = load("quic_control_coordinator", ROOT / "coordinator/coordinator.py")
        self.c.DB_PATH = str(Path(self.temp.name) / "db.sqlite"); self.c._init_db()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.c.app), base_url="http://test")
        for number in (1, 2):
            response = await self.client.post("/register", headers={"X-API-Key": "agent"}, json={
                "agent_id": f"gpu-{number}", "hostname": f"host-{number}", "port": 11434,
                "model": "qwen3:14b", "gpu_vram_gb": 12, "lan_ip": f"10.0.0.{number}"})
            self.assertEqual(response.status_code, 200, response.text)
            self.c.machines[f"gpu-{number}"].runtime = {"rpc_quic": True}

    async def asyncTearDown(self):
        await self.client.aclose(); self.temp.cleanup()

    def form(self):
        cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19, transport="quic", ctx_size=32768)
        self.assertIsNotNone(cluster)
        return cluster

    async def publish(self, cluster, identity, **overrides):
        certificate = "-----BEGIN CERTIFICATE-----\npublic-test-certificate\n-----END CERTIFICATE-----\n" if identity in cluster.worker_ids else ""
        body = {"agent_id": identity, "cluster_id": cluster.cluster_id, "port": 12345,
                "certificate_pem": certificate, **overrides}
        return await self.client.post("/agent/rpc/quic/endpoint", json=body,
            headers={"X-API-Key": "agent", "X-Swarm-Agent-ID": identity})

    async def test_pair_exchange_excludes_secrets_from_public_status_and_never_provisions_cloud(self):
        cluster = self.form(); primary = cluster.primary_id; worker = cluster.worker_ids[0]
        p = self.c.agent_rpc_assignments[primary]; w = self.c.agent_rpc_assignments[worker]
        self.assertEqual(p["quic_token"], w["quic_token"])
        self.assertEqual(len(p["quic_token"]), 64)
        self.assertIsNone(cluster.relay_url)
        self.assertEqual(w["listen_host"], "127.0.0.1")
        self.assertEqual((await self.publish(cluster, primary)).json(), {"peers": []})
        response = await self.publish(cluster, worker, stats={"sent_bytes": 123, "connected": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["peers"][0]["ip"], "10.0.0.1")
        response = await self.publish(cluster, primary)
        self.assertIn("BEGIN CERTIFICATE", response.json()["peers"][0]["certificate_pem"])
        status = (await self.client.get("/machines", headers={"X-API-Key": "admin"})).text
        self.assertNotIn(p["quic_token"], status)
        self.assertNotIn("public-test-certificate", status)
        self.assertEqual(cluster.to_dict()["quic_stats"][worker]["sent_bytes"], 123)

    async def test_legacy_agent_and_invalid_address_cannot_reserve_quic_gpus(self):
        self.c.machines["gpu-2"].runtime = {}
        self.assertIsNone(self.c._form_rpc_cluster("qwen3:30b-a3b", 19, transport="quic"))
        self.assertFalse(any(m.in_rpc for m in self.c.machines.values()))
        self.c.machines["gpu-2"].runtime = {"rpc_quic": True}
        self.c.machines["gpu-2"].lan_ip = "8.8.8.8"
        self.assertIsNone(self.c._form_rpc_cluster("qwen3:30b-a3b", 19, transport="quic"))

    async def test_endpoints_are_bounded_immutable_and_rejected_after_dissolve(self):
        cluster = self.form(); worker = cluster.worker_ids[0]
        self.assertEqual((await self.publish(cluster, worker)).status_code, 200)
        self.assertEqual((await self.publish(cluster, worker, port=12346)).status_code, 409)
        self.assertEqual((await self.publish(cluster, worker, port=0)).status_code, 422)
        self.assertEqual((await self.publish(cluster, worker, certificate_pem="x"*5000)).status_code, 422)
        self.assertEqual((await self.publish(cluster, worker, stats={"peak_inflight_chunks": 5})).status_code, 422)
        self.c._dissolve_cluster(cluster, "test")
        self.assertEqual((await self.publish(cluster, worker)).status_code, 410)

    async def test_wrong_identity_and_consumer_key_cannot_publish(self):
        cluster = self.form()
        for headers in ({"X-API-Key": "consumer", "X-Swarm-Agent-ID": cluster.primary_id},
                        {"X-API-Key": "agent", "X-Swarm-Agent-ID": "outsider"}):
            response = await self.client.post("/agent/rpc/quic/endpoint", headers=headers,
                json={"agent_id": cluster.primary_id, "cluster_id": cluster.cluster_id, "port": 12345})
            self.assertIn(response.status_code, (401, 403))

    async def test_quic_trial_cannot_silently_reuse_a_relay_cluster(self):
        cluster = self.form(); cluster.transport = "relay"
        response = await self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"}, json={
            "model": cluster.model, "rpc_transport": "quic", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.c.jobs)

    async def test_pinned_pair_ignores_new_larger_gpu_and_preserves_its_capacity(self):
        response = await self.client.post("/register", headers={"X-API-Key": "agent"}, json={
            "agent_id": "new-gpu", "hostname": "new-host", "port": 11434, "model": "qwen3:14b",
            "gpu_vram_gb": 24, "lan_ip": "10.0.0.3"})
        self.assertEqual(response.status_code, 200)
        self.c.machines["new-gpu"].runtime = {"rpc_quic": True}
        cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19, transport="quic", agent_ids=["gpu-1", "gpu-2"])
        self.assertEqual({cluster.primary_id, *cluster.worker_ids}, {"gpu-1", "gpu-2"})
        self.assertFalse(self.c.machines["new-gpu"].in_rpc)

    async def test_invalid_or_unprivileged_pinning_is_rejected_before_reservation(self):
        for key, ids, expected in (("consumer", ["gpu-1", "gpu-2"], 403),
                                   ("admin", ["gpu-1", "gpu-1"], 422),
                                   ("admin", ["gpu-1"], 422)):
            response = await self.client.post("/v1/chat/completions", headers={"X-API-Key": key}, json={
                "model": "qwen3:30b-a3b", "rpc_transport": "quic", "rpc_agent_ids": ids,
                "messages": [{"role": "user", "content": "hello"}]})
            self.assertEqual(response.status_code, expected, response.text)
            self.assertFalse(self.c.rpc_clusters)
