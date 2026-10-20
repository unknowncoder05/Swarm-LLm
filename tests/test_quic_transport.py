"""Exercise actual encrypted QUIC, packet faults, bounded buffers and TLS/auth."""
import asyncio
import copy
import os
import threading
import time
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives import serialization
from test_agent_reports import ROOT, load


class FaultProxy(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.peer_transport = None
        self.destination = None
        self.packets = 0
    def connection_made(self, transport):
        self.transport = transport
    def datagram_received(self, data, address):
        self.packets += 1
        if self.destination is None or self.packets % 17 == 0:
            return
        def forward():
            if not self.peer_transport.is_closing():
                self.peer_transport.sendto(data, self.destination)
        if self.packets % 11 == 0:
            asyncio.get_running_loop().call_later(.01, forward)
        else:
            forward()
        if self.packets % 31 == 0:
            forward()


class QuicTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.q = load("quic_transport_under_test", ROOT / "agent/quic_transport.py")
        self.tunnels = []; self.proxies = []; self.native_connections = 0
        self.native = await asyncio.start_server(self.echo, "127.0.0.1", 0)

    async def asyncTearDown(self):
        await asyncio.gather(*(asyncio.to_thread(t.close) for t in self.tunnels))
        self.native.close(); await self.native.wait_closed()
        for proxy in self.proxies:
            proxy.transport.close()
        await asyncio.sleep(.05)

    async def echo(self, reader, writer):
        self.native_connections += 1
        try:
            while data := await reader.read(65536):
                writer.write(data); await writer.drain()
        finally:
            writer.close()

    async def pair(self, faults=False, wrong_token=False, wrong_cert=False, legacy_worker=False, legacy_primary=False):
        publications = {}; lock = threading.Lock()
        if faults:
            loop = asyncio.get_running_loop()
            for _ in range(2):
                proxy = FaultProxy()
                await loop.create_datagram_endpoint(lambda: proxy, local_addr=("127.0.0.1", 0))
                self.proxies.append(proxy)
            self.proxies[0].peer_transport = self.proxies[1].transport
            self.proxies[1].peer_transport = self.proxies[0].transport
        bad_pem = self.q.server_identity()[1].public_bytes(serialization.Encoding.PEM).decode() if wrong_cert else None
        def control(payload):
            with lock:
                publications[payload["agent_id"]] = copy.deepcopy(payload)
                other = "worker" if payload["agent_id"] == "primary" else "primary"
                peer = publications.get(other)
                if not peer:
                    return {"peers": []}
                port = peer["port"]
                if faults:
                    primary, worker = self.proxies
                    primary.destination = ("127.0.0.1", publications["primary"]["port"])
                    worker.destination = ("127.0.0.1", publications["worker"]["port"])
                    port = (worker if other == "worker" else primary).transport.get_extra_info("sockname")[1]
                return {"peers": [{"agent_id": other, "ip": "127.0.0.1", "port": port,
                    "certificate_pem": bad_pem if wrong_cert and other == "worker" else peer["certificate_pem"]}]}
        common = {"cluster_id": "trial", "lan_ip": "127.0.0.1", "quic_token": "a" * 64}
        worker = self.q.QuicTunnel(dict(common, role="worker"), "worker", control,
                                  native_port=self.native.sockets[0].getsockname()[1],
                                  alpn_protocols=[self.q.ALPN] if legacy_worker else None)
        primary = self.q.QuicTunnel(dict(common, role="primary", quic_token=("b" * 64 if wrong_token else "a" * 64)),
                                   "primary", control, alpn_protocols=[self.q.ALPN] if legacy_primary else None)
        self.tunnels.extend([worker, primary])
        await asyncio.gather(asyncio.to_thread(worker.start, 15), asyncio.to_thread(primary.start, 15))
        return primary, worker

    async def test_large_bidirectional_payload_survives_loss_reordering_duplicates_and_half_close(self):
        primary, worker = await self.pair(faults=True)
        host, port = primary.endpoint.split(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        payload = os.urandom(2 * 1024 * 1024 + 137)
        async def send():
            writer.write(payload); await writer.drain(); writer.write_eof()
        sender = asyncio.create_task(send())
        received = await asyncio.wait_for(reader.readexactly(len(payload)), 30)
        self.assertEqual(received, payload)
        self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"")
        await sender
        writer.close(); await writer.wait_closed()
        for tunnel in (primary, worker):
            self.assertIsNone(tunnel.error)
            self.assertLessEqual(tunnel.stats["peak_inflight_chunks"], 4)
            self.assertEqual(tunnel.stats["sent_bytes"], len(payload))
            self.assertEqual(tunnel.stats["received_bytes"], len(payload))
        self.assertTrue(all(p.packets > 31 for p in self.proxies))
        self.assertEqual(primary.profile, self.q.ALPN_BULK)
        self.assertEqual(worker.profile, self.q.ALPN_BULK)
        self.assertEqual(primary._protocol.chunk_size, 256*1024)

    async def test_wrong_stream_token_never_opens_native_rpc(self):
        primary, worker = await self.pair(wrong_token=True)
        host, port = primary.endpoint.split(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(b"unauthorized"); await writer.drain()
        try:
            self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"")
        except ConnectionResetError:
            pass  # A reset and EOF both reject the unauthenticated stream.
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            pass
        for _ in range(50):
            if primary.error:break
            await asyncio.sleep(.01)
        self.assertEqual(self.native_connections, 0)
        self.assertIsNotNone(primary.error)
        self.assertNotIn("a" * 64, primary.error)

    async def test_untrusted_tls_certificate_rejects_connection(self):
        with self.assertRaises(RuntimeError):
            await self.pair(wrong_cert=True)
        self.assertEqual(self.native_connections, 0)

    async def test_worker_is_ready_while_primary_downloads_and_closes_without_a_peer(self):
        publications = []
        def control(payload):
            publications.append(payload)
            return {"peers": []}
        worker = self.q.QuicTunnel({"cluster_id": "cold", "role": "worker", "lan_ip": "127.0.0.1",
                                   "quic_token": "a"*64}, "worker", control,
                                  native_port=self.native.sockets[0].getsockname()[1])
        self.tunnels.append(worker)
        await asyncio.to_thread(worker.start, 3)
        self.assertTrue(publications)
        self.assertIsNone(worker.error)
        self.assertEqual(self.native_connections, 0)
        await asyncio.to_thread(worker.close)
        self.assertFalse(worker._thread.is_alive())

    async def legacy_transfer(self, **options):
        primary, worker = await self.pair(**options)
        host, port = primary.endpoint.split(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        payload = os.urandom(512*1024+17)
        writer.write(payload); await writer.drain(); writer.write_eof()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(payload)), 15), payload)
        self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"")
        writer.close(); await writer.wait_closed()
        self.assertEqual(primary.profile, self.q.ALPN)
        self.assertEqual(worker.profile, self.q.ALPN)
        self.assertEqual(primary._protocol.chunk_size, 64*1024)
        self.assertIsNone(primary.error); self.assertIsNone(worker.error)

    async def test_new_primary_falls_back_to_legacy_worker_profile(self):
        await self.legacy_transfer(legacy_worker=True)

    async def test_new_worker_falls_back_to_legacy_primary_profile(self):
        await self.legacy_transfer(legacy_primary=True)
