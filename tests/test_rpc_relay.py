"""Exercise raw byte relay ordering, retransmission, flow control and teardown."""
import asyncio
import importlib.util
import os
import socket
import threading
import time
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import httpx
import requests

ROOT = Path(__file__).resolve().parents[1]


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"ADMIN_KEY": "relay-admin", "AGENT_KEY": "relay-agent",
                                     "CONSUMER_KEY": "relay-consumer"}):
            spec = importlib.util.spec_from_file_location("relay_test_coordinator", ROOT / "coordinator/coordinator.py")
            self.coordinator = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.coordinator)
        c = self.coordinator
        c.DB_PATH = str(Path(self.temp.name) / "coordinator.db")
        c._init_db()
        c.rpc_clusters["trial"] = c.RpcCluster("trial", "qwen3:30b-a3b", "primary", ["worker"], transport="relay")
        c.agent_rpc_assignments["worker"] = {"role": "worker", "transport": "relay", "cluster_id": "trial"}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url="http://test")
        self.primary = {"X-API-Key": "relay-agent", "X-Swarm-Agent-ID": "primary"}
        self.worker = {"X-API-Key": "relay-agent", "X-Swarm-Agent-ID": "worker"}
        self.connection_id = str(uuid.uuid4())
        self.base = f"/agent/rpc/relay/trial/worker/{self.connection_id}"
        self.assertEqual((await self.client.post(self.base, headers=self.primary)).status_code, 200)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def send(self, data, seq=0, side="primary"):
        return await self.client.post(self.base + f"/{side}/send?seq={seq}", content=data,
                                     headers=self.primary if side == "primary" else self.worker)

    async def recv(self, ack=-1, side="worker"):
        return await self.client.get(self.base + f"/{side}/recv?ack={ack}",
                                    headers=self.worker if side == "worker" else self.primary)

    async def test_bytes_round_trip_and_lost_http_responses_do_not_duplicate_data(self):
        tensor = b"\0\xff\x80tensor" * 8192
        self.assertEqual((await self.send(tensor)).status_code, 200)
        self.assertEqual((await self.send(tensor)).status_code, 200)
        received = await self.recv()
        self.assertEqual(received.content, tensor)
        self.assertEqual(received.headers["X-Relay-Sequence"], "0")
        self.assertEqual((await self.recv()).content, tensor)  # response was lost; retry
        await self.send(b"next tensor", seq=1)
        self.assertEqual((await self.recv(ack=0)).content, b"next tensor")
        await self.send(b"CUDA allocation reply", side="worker")
        self.assertEqual((await self.recv(side="primary")).content, b"CUDA allocation reply")
        self.assertEqual(self.coordinator.rpc_clusters["trial"].relay_bytes,
                         len(tensor) + len(b"next tensor") + len(b"CUDA allocation reply"))

    async def test_wrong_agent_and_consumer_key_cannot_access_the_channel(self):
        for headers in ({"X-API-Key": "relay-consumer", "X-Swarm-Agent-ID": "primary"},
                        {"X-API-Key": "relay-agent", "X-Swarm-Agent-ID": "outsider"}, {}):
            response = await self.client.post(self.base + "/primary/send?seq=0", content=b"bad", headers=headers)
            self.assertIn(response.status_code, (401, 403))

    async def test_bad_sequences_oversized_frames_and_changed_retries_are_rejected(self):
        self.assertEqual((await self.send(b"bad", seq=1)).status_code, 409)
        self.assertEqual((await self.send(b"x" * (self.coordinator.RPC_RELAY_FRAME + 1))).status_code, 413)
        self.assertEqual((await self.send(b"valid")).status_code, 200)
        self.assertEqual((await self.send(b"changed")).status_code, 409)
        self.assertEqual((await self.recv()).content, b"valid")

    async def test_bounded_queue_applies_backpressure_until_receiver_drains(self):
        await self.send(b"one", seq=0)
        await self.send(b"two", seq=1)
        third = asyncio.create_task(self.send(b"three", seq=2))
        await asyncio.sleep(.05)
        self.assertFalse(third.done())
        self.assertEqual((await self.recv()).content, b"one")
        self.assertEqual((await asyncio.wait_for(third, timeout=1)).status_code, 200)
        self.assertEqual((await self.recv(ack=0)).content, b"two")
        self.assertEqual((await self.recv(ack=1)).content, b"three")

    async def test_close_unblocks_pending_poll_and_removes_the_connection(self):
        poll = asyncio.create_task(self.recv())
        await asyncio.sleep(.05)
        await self.client.post(self.base + "/primary/close", headers=self.primary)
        self.assertEqual((await asyncio.wait_for(poll, timeout=1)).status_code, 410)
        self.assertEqual(self.coordinator.rpc_relays, {})

    async def test_close_unblocks_sender_waiting_on_full_queue(self):
        await self.send(b"one", seq=0)
        await self.send(b"two", seq=1)
        blocked = asyncio.create_task(self.send(b"three", seq=2))
        await asyncio.sleep(.05)
        self.assertFalse(blocked.done())
        await self.client.post(self.base + "/worker/close", headers=self.worker)
        self.assertEqual((await asyncio.wait_for(blocked, timeout=1)).status_code, 410)
        self.assertEqual(self.coordinator.rpc_relays, {})

    async def test_worker_discovers_channels_without_an_inbound_connection(self):
        response = await self.client.get("/agent/rpc/assignment?agent_id=worker", headers=self.worker)
        self.assertEqual(response.json()["relay_connections"], [self.connection_id])
        self.coordinator._dissolve_cluster(self.coordinator.rpc_clusters["trial"], "test finished")
        self.assertEqual((await self.recv()).status_code, 410)
        self.assertEqual(self.coordinator.rpc_relays, {})

    async def lifecycle_tick(self):
        calls = 0
        async def tick(_):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise asyncio.CancelledError()
        with patch.object(self.coordinator.asyncio, "sleep", side_effect=tick):
            with self.assertRaises(asyncio.CancelledError):
                await self.coordinator._rpc_lifecycle_loop()

    def members(self, primary_age=0):
        c = self.coordinator
        for name, age in (("primary", primary_age), ("worker", 0)):
            c.machines[name] = SimpleNamespace(last_heartbeat=time.time() - age, busy=False,
                busy_job_id=None, in_rpc=True, rpc_cluster_id="trial")
        c.agent_rpc_assignments["primary"] = {"role": "primary", "cluster_id": "trial"}

    async def test_missing_member_fails_waiting_job_and_releases_relay(self):
        c = self.coordinator
        self.members(primary_age=c.DEAD_AFTER_SEC + 1)
        job = c.Job({}, "qwen3:30b-a3b")
        c.jobs[job.id] = job
        await self.lifecycle_tick()
        cluster = c.rpc_clusters["trial"]
        self.assertEqual(cluster.status, "dissolving")
        self.assertEqual(cluster.dissolve_reason, "member_offline")
        self.assertTrue(job.done_event.is_set())
        self.assertEqual(job.error, "cancelled:rpc_member_offline")
        self.assertTrue(c.agent_rpc_assignments["worker"]["dissolve"])
        self.assertEqual(c.rpc_relays, {})

    async def test_old_cluster_gets_teardown_grace_and_cannot_be_revived(self):
        c = self.coordinator
        self.members()
        cluster = c.rpc_clusters["trial"]
        cluster.formed_at = time.time() - 4000
        c._dissolve_cluster(cluster, "member_offline")
        await self.lifecycle_tick()
        self.assertIn("trial", c.rpc_clusters)
        response = await self.client.post("/agent/rpc/ready", headers=self.primary,
            json={"agent_id": "primary", "cluster_id": "trial", "role": "primary"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(cluster.status, "dissolving")

    async def test_pending_work_prevents_idle_teardown(self):
        c = self.coordinator
        self.members()
        cluster = c.rpc_clusters["trial"]
        cluster.status = "active"
        cluster.last_job_at = time.time() - c.RPC_IDLE_TIMEOUT - 1
        job = c.Job({}, "qwen3:30b-a3b")
        c.jobs[job.id] = job
        await self.lifecycle_tick()
        self.assertEqual(cluster.status, "active")
        self.assertFalse(job.done_event.is_set())

    async def test_connection_count_is_bounded(self):
        for _ in range(self.coordinator.RPC_RELAY_CONNECTIONS - 1):
            path = f"/agent/rpc/relay/trial/worker/{uuid.uuid4()}"
            self.assertEqual((await self.client.post(path, headers=self.primary)).status_code, 200)
        path = f"/agent/rpc/relay/trial/worker/{uuid.uuid4()}"
        self.assertEqual((await self.client.post(path, headers=self.primary)).status_code, 429)

    async def test_configured_relay_default_does_not_require_client_transport_field(self):
        c = self.coordinator
        c.rpc_clusters.clear()
        c.RPC_DEFAULT_TRANSPORT = "relay"
        from fastapi import HTTPException
        for override in (None, "direct"):
            body = {"model": "qwen3:30b-a3b", "messages": [{"role": "user", "content": "hi"}]}
            if override:
                body["rpc_transport"] = override
            with patch.object(c, "_form_rpc_cluster", side_effect=HTTPException(503, "test admission")) as form:
                response = await self.client.post("/v1/chat/completions", headers={"X-API-Key": "relay-consumer"}, json=body)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(form.call_args.kwargs["transport"], override or "relay")

    async def test_agent_socket_bridges_preserve_large_binary_payload_in_both_directions(self):
        loop = asyncio.get_running_loop()
        client = self.client
        lost = {"send_ack": False, "receive_response": False}
        def forward_request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            if "data" in kwargs:
                kwargs["content"] = kwargs.pop("data")
            response = asyncio.run_coroutine_threadsafe(client.request(method, url, **kwargs), loop).result(timeout=35)
            # Simulate network failure after the server has accepted a frame or
            # selected a response. Retrying must not duplicate or lose bytes.
            if response.status_code == 200:
                fault = "send_ack" if url.endswith("/send") else "receive_response" if url.endswith("/recv") else None
                if fault and not lost[fault]:
                    lost[fault] = True
                    raise requests.exceptions.ConnectionError("Simulated lost relay response")
            return response
        class Session:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            request = staticmethod(forward_request)
        agents = []
        for name in ("primary", "worker"):
            spec = importlib.util.spec_from_file_location("relay_" + name, ROOT / "agents/agent.py")
            agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(agent)
            agent._agent_id = name
            agents.append(agent)
        primary_app, primary_socket = socket.socketpair()
        worker_app, worker_socket = socket.socketpair()
        for sock in (primary_app, worker_app):
            sock.settimeout(15)
        primary_bridge = agents[0]._RpcRelayBridge(primary_socket, "http://test", "relay-agent",
            "trial", "worker", self.connection_id, "primary", "qwen3:30b-a3b")
        worker_bridge = agents[1]._RpcRelayBridge(worker_socket, "http://test", "relay-agent",
            "trial", "worker", self.connection_id, "worker", "qwen3:30b-a3b")
        failures = []
        def read_exact(sock, count):
            chunks = []
            while count:
                chunk = sock.recv(min(count, 65536))
                if not chunk:
                    raise RuntimeError("Socket closed before complete payload")
                chunks.append(chunk)
                count -= len(chunk)
            return b"".join(chunks)
        payload = os.urandom(2 * self.coordinator.RPC_RELAY_FRAME + 37)
        def echo():
            incoming = read_exact(worker_app, len(payload))
            worker_app.sendall(incoming[::-1])
            return incoming
        threads = [threading.Thread(target=bridge.run, daemon=True) for bridge in (primary_bridge, worker_bridge)]
        with patch.object(agents[0].requests, "Session", Session), \
             patch.object(agents[0].requests, "post", side_effect=lambda url, **kwargs: forward_request("POST", url, **kwargs)), \
             patch.object(agents[0], "_log_crash", side_effect=failures.append), \
             patch.object(agents[1], "_log_crash", side_effect=failures.append):
            for thread in threads:
                thread.start()
            try:
                echoed = asyncio.create_task(asyncio.to_thread(echo))
                received = asyncio.create_task(asyncio.to_thread(read_exact, primary_app, len(payload)))
                await asyncio.to_thread(primary_app.sendall, payload)
                self.assertEqual(await asyncio.wait_for(echoed, timeout=15), payload)
                self.assertEqual(await asyncio.wait_for(received, timeout=15), payload[::-1])
            finally:
                await asyncio.to_thread(primary_bridge.close)
                await asyncio.to_thread(worker_bridge.close)
                primary_app.close()
                worker_app.close()
                for thread in threads:
                    await asyncio.to_thread(thread.join, 2)
            self.assertEqual(failures, [])
            self.assertTrue(all(lost.values()))


class RelaySocketTests(unittest.TestCase):
    def test_tcp_bridge_disables_nagle_on_connected_and_accepted_sockets(self):
        spec = importlib.util.spec_from_file_location("relay_tcp_agent", ROOT / "agents/agent.py")
        agent = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(agent)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with socket.create_connection(listener.getsockname()) as client:
                peer, _ = listener.accept()
                with peer:
                    for sock, side in ((client, "worker"), (peer, "primary")):
                        self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 0)
                        agent._RpcRelayBridge(sock, "http://test", "relay-agent", "trial",
                                              "worker", "connection", side, "qwen3:30b-a3b")
                        self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1)


if __name__ == "__main__":
    unittest.main()
