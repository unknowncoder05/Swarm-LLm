"""Exercise the regional data plane and coordinator integration."""
import asyncio
import importlib.util
import os
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import httpx
import test_rpc_relay as transport_tests

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RegionalRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.dict(os.environ, {"AGENT_KEY": "relay-agent", "CONTROL_KEY": "relay-control", "RELAY_KEYS_FILE": ""}):
            self.server = load("regional_test_server", "coordinator/relay_server.py")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="https://regional")
        self.control = {"X-API-Key": "relay-control"}
        self.primary = {"X-API-Key": "relay-agent", "X-Swarm-Agent-ID": "primary"}
        self.worker = {"X-API-Key": "relay-agent", "X-Swarm-Agent-ID": "worker"}
        self.coordinator = SimpleNamespace(RPC_RELAY_FRAME=768*1024,
            rpc_relays=self.server.connections, rpc_clusters=self.server.clusters)
        self.connection_id = str(uuid.uuid4())
        self.base = f"/agent/rpc/relay/trial/worker/{self.connection_id}"
        response = await self.client.put("/control/clusters/trial", headers=self.control,
                                        json={"primary_id": "primary", "worker_ids": ["worker"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.client.post(self.base, headers=self.primary)).status_code, 200)

    async def asyncTearDown(self):
        await self.client.aclose()

    send = transport_tests.RelayTests.send
    recv = transport_tests.RelayTests.recv
    test_lost_responses_preserve_bytes = transport_tests.RelayTests.test_bytes_round_trip_and_lost_http_responses_do_not_duplicate_data
    test_access_control = transport_tests.RelayTests.test_wrong_agent_and_consumer_key_cannot_access_the_channel
    test_bounds_and_sequences = transport_tests.RelayTests.test_bad_sequences_oversized_frames_and_changed_retries_are_rejected
    test_backpressure = transport_tests.RelayTests.test_bounded_queue_applies_backpressure_until_receiver_drains
    test_close_wakes_receiver = transport_tests.RelayTests.test_close_unblocks_pending_poll_and_removes_the_connection
    test_close_wakes_sender = transport_tests.RelayTests.test_close_unblocks_sender_waiting_on_full_queue
    test_actual_socket_transfer_with_retries = transport_tests.RelayTests.test_agent_socket_bridges_preserve_large_binary_payload_in_both_directions

    async def test_agent_key_cannot_register_read_or_delete_control_state(self):
        for method in ("PUT", "GET", "DELETE"):
            response = await self.client.request(method, "/control/clusters/trial", headers=self.primary,
                json={"primary_id": "primary", "worker_ids": ["worker"]} if method == "PUT" else None)
            self.assertEqual(response.status_code, 401)
        self.assertEqual((await self.client.post(self.base, headers=self.control)).status_code, 401)

    async def test_expired_lease_rejects_data_and_control_poll_cleans_connections(self):
        self.server.clusters["trial"].expires_at = time.time() - 1
        self.assertEqual((await self.send(b"expired")).status_code, 410)
        self.assertEqual((await self.client.get("/control/clusters/trial", headers=self.control)).status_code, 410)
        self.assertEqual(self.server.connections, {})
        self.assertEqual(self.server.clusters, {})

    async def test_background_expiry_removes_orphans_without_another_request(self):
        connection = next(iter(self.server.connections.values()))
        self.server.clusters["trial"].expires_at = time.time() - 1
        calls = 0
        async def tick(_):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise asyncio.CancelledError()
        with patch.object(self.server.asyncio, "sleep", side_effect=tick):
            with self.assertRaises(asyncio.CancelledError):
                await self.server.expire_clusters()
        self.assertTrue(connection.closed.is_set())
        self.assertEqual(self.server.connections, {})
        self.assertEqual(self.server.clusters, {})

    async def test_membership_cannot_change_and_deletion_wakes_waiters(self):
        response = await self.client.put("/control/clusters/trial", headers=self.control,
                                        json={"primary_id": "other", "worker_ids": ["worker"]})
        self.assertEqual(response.status_code, 409)
        poll = asyncio.create_task(self.recv())
        await asyncio.sleep(.02)
        self.assertEqual((await self.client.delete("/control/clusters/trial", headers=self.control)).status_code, 200)
        self.assertEqual((await asyncio.wait_for(poll, 1)).status_code, 410)

    async def test_coordinator_discovers_remote_channels_without_forwarding_tensor_bytes(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "coordinator-admin", "AGENT_KEY": "relay-agent",
                                     "CONSUMER_KEY": "coordinator-consumer", "RPC_RELAY_URL": "", "RPC_RELAY_ADMIN_KEY": ""}):
            c = load("regional_test_coordinator", "coordinator/coordinator.py")
        cluster = c.RpcCluster("trial", "qwen3:30b-a3b", "primary", ["worker"], transport="relay")
        cluster.relay_url, cluster.relay_ready = "https://regional", False
        c.rpc_clusters["trial"] = cluster
        c.agent_rpc_assignments["worker"] = {"role": "worker", "cluster_id": "trial", "transport": "relay", "relay_url": cluster.relay_url}
        loop = asyncio.get_running_loop()

        def remote_call(cluster, method, payload=None):
            response = asyncio.run_coroutine_threadsafe(self.client.request(method,
                "/control/clusters/trial", headers=self.control, json=payload), loop).result(timeout=3)
            response.raise_for_status()
            result = response.json()
            cluster.relay_last_ok = time.time()
            cluster.relay_bytes = result.get("relay_bytes", cluster.relay_bytes)
            return result

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url="http://coordinator") as client:
            path = "/agent/rpc/assignment?agent_id=worker"
            self.assertEqual((await client.get(path, headers=self.worker)).status_code, 204)
            cluster.relay_ready = True
            with patch.object(c, "_regional_relay_call", side_effect=remote_call):
                result = (await client.get(path, headers=self.worker)).json()
                self.assertEqual(result["relay_connections"], [self.connection_id])
                self.assertEqual(result["relay_url"], "https://regional")
                await self.send(b"tensor goes directly to Spain")
                self.assertEqual((await self.recv()).content, b"tensor goes directly to Spain")
                await client.get(path, headers=self.worker)
                self.assertGreater(cluster.relay_bytes, 0)
                self.assertEqual(c.rpc_relays, {})

    async def test_regional_formation_requires_capable_agents(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "coordinator-admin", "AGENT_KEY": "relay-agent",
                                     "CONSUMER_KEY": "coordinator-consumer", "RPC_RELAY_URL": "", "RPC_RELAY_ADMIN_KEY": ""}):
            c = load("regional_formation_coordinator", "coordinator/coordinator.py")
        c.RPC_RELAY_URL = "https://regional"
        for index, name in enumerate(("primary", "worker")):
            c.machines[name] = SimpleNamespace(agent_id=name, hostname=name, alive=True, in_rpc=False,
                busy=False, media_phase="IDLE", gpu_vram_gb=12, runtime={}, lan_ip=f"10.0.0.{index + 1}", ip="10.0.0.1")
        self.assertIsNone(c._form_rpc_cluster("qwen3:30b-a3b", 20, transport="relay"))
        for machine in c.machines.values():
            machine.runtime["rpc_relay_external"] = True
        cluster = c._form_rpc_cluster("qwen3:30b-a3b", 20, transport="relay")
        self.assertEqual(cluster.relay_url, "https://regional")
        self.assertFalse(cluster.relay_ready)
        # After the instance's budget deadline, new jobs use the existing local
        # relay, including agents that do not support an external endpoint.
        for machine in c.machines.values():
            machine.in_rpc = False
            machine.runtime = {}
        c.RPC_RELAY_EXPIRES_AT = time.time() - 1
        fallback = c._form_rpc_cluster("qwen3:30b-a3b", 20, transport="relay")
        self.assertIsNone(fallback.relay_url)
        self.assertTrue(fallback.relay_ready)



if __name__ == "__main__":
    unittest.main()
