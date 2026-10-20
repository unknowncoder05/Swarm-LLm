"""Bounded native RPC TCP streams over authenticated, encrypted LAN QUIC.

The coordinator supplies pair membership and certificates over its existing
authenticated HTTPS channel. It never handles the model's QUIC payloads.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import hmac
import ssl
import struct
import threading
import time

from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.asyncio.server import QuicServer
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import HandshakeCompleted
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ALPN = "swarm-rpc-v1"
ALPN_BULK = "swarm-rpc-v2"
CHUNK = 64 * 1024
BULK_CHUNK = 256 * 1024
WINDOW = 4
MAX_STREAMS = 8
HEADER = struct.Struct("!BI")
DATA, ACK, EOF = range(3)
PUNCH = b"SWARM_QUIC_PUNCH_V1"


def server_identity():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "swarm-rpc")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("swarm-rpc")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


async def bridge_stream(tcp_reader, tcp_writer, quic_reader, quic_writer, stats, chunk_size=CHUNK):
    """Application credits bound aioquic's send buffers and native TCP reads."""
    credits = asyncio.BoundedSemaphore(WINDOW)
    outstanding = 0
    local_finished = asyncio.Event()
    def frame(kind, data=b""):
        quic_writer.write(HEADER.pack(kind, len(data)) + data)
    async def send():
        nonlocal outstanding
        while True:
            data = await tcp_reader.read(chunk_size)
            if not data:
                # Finish this direction only after its bytes reached the peer's
                # native socket. Continue receiving credits for the other side.
                for _ in range(WINDOW):
                    await credits.acquire()
                frame(EOF)
                local_finished.set()
                return
            await credits.acquire()
            outstanding += 1
            stats["peak_inflight_chunks"] = max(stats["peak_inflight_chunks"], outstanding)
            frame(DATA, data)
            stats["sent_bytes"] += len(data)
    async def receive():
        nonlocal outstanding
        remote_finished = False
        while True:
            if remote_finished:
                if local_finished.is_set():
                    return
                header = asyncio.create_task(quic_reader.readexactly(HEADER.size))
                finished = asyncio.create_task(local_finished.wait())
                try:
                    done, _ = await asyncio.wait((header, finished), return_when=asyncio.FIRST_COMPLETED)
                    if header not in done:
                        return
                    raw = header.result()
                finally:
                    header.cancel(); finished.cancel()
                    await asyncio.gather(header, finished, return_exceptions=True)
            else:
                raw = await quic_reader.readexactly(HEADER.size)
            kind, size = HEADER.unpack(raw)
            if kind == DATA and 0 < size <= chunk_size and not remote_finished:
                data = await quic_reader.readexactly(size)
                tcp_writer.write(data)
                await tcp_writer.drain()
                stats["received_bytes"] += size
                frame(ACK)
            elif kind == ACK and size == 0 and outstanding > 0:
                outstanding -= 1
                credits.release()
            elif kind == EOF and size == 0:
                if tcp_writer.can_write_eof():
                    tcp_writer.write_eof()
                remote_finished = True
            else:
                raise RuntimeError("Invalid QUIC RPC frame or flow-control acknowledgement")
    tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        tcp_writer.close()
        quic_writer.close()


class _PeerServer(QuicServer):
    def __init__(self, owner, **kwargs):
        super().__init__(**kwargs)
        self.owner = owner
    def datagram_received(self, data, addr):
        if addr != self.owner.peer_address or data == PUNCH:
            return
        super().datagram_received(data, addr)


class _StreamProtocol(QuicConnectionProtocol):
    def __init__(self, quic, stream_handler=None):
        super().__init__(quic, stream_handler=stream_handler)
        self.chunk_size = CHUNK
        self.profile = None
    def quic_event_received(self, event):
        if isinstance(event, HandshakeCompleted):
            self.profile = event.alpn_protocol
            self.chunk_size = BULK_CHUNK if self.profile == ALPN_BULK else CHUNK
        super().quic_event_received(event)


class _PeerClient(_StreamProtocol):
    def __init__(self, quic, peer_address):
        super().__init__(quic)
        self.peer_address = peer_address
    def datagram_received(self, data, addr):
        if addr == self.peer_address and data != PUNCH:
            super().datagram_received(data, addr)


class QuicTunnel:
    """One assigned worker/primary pair, owned and torn down by its agent role."""
    def __init__(self, assignment, agent_id, control, native_port=None, alpn_protocols=None):
        self.assignment, self.agent_id, self.control = assignment, agent_id, control
        self.native_port = native_port
        self.alpn_protocols = alpn_protocols or [ALPN_BULK, ALPN]
        self.profile = None
        self.peer_address = None
        self.stats = {"sent_bytes": 0, "received_bytes": 0, "streams": 0,
                      "peak_inflight_chunks": 0, "connected": False}
        self.error = None
        self.endpoint = None
        self._ready = threading.Event()
        self._thread = None
        self._loop = None
        self._stopped = None
        self._tasks = set()

    def start(self, timeout=180):
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="rpc-quic")
        self._thread.start()
        if not self._ready.wait(timeout):
            self.close()
            raise RuntimeError("LAN QUIC startup timed out")
        self.check()
        return self

    def check(self):
        if self.error:
            raise RuntimeError("LAN QUIC transport failed: " + self.error)

    def close(self):
        if self._loop and self._stopped and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stopped.set)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self.error = type(exc).__name__ + ": " + str(exc)[:250]
        finally:
            self._ready.set()

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        def finished(done):
            self._tasks.discard(done)
            if not done.cancelled() and done.exception():
                self.error = type(done.exception()).__name__ + ": " + str(done.exception())[:250]
                self._stopped.set()
        task.add_done_callback(finished)

    async def _run(self):
        self._loop = asyncio.get_running_loop()
        self._stopped = asyncio.Event()
        is_worker = self.assignment["role"] == "worker"
        config = QuicConfiguration(is_client=not is_worker, alpn_protocols=self.alpn_protocols,
                                   congestion_control_algorithm="cubic", idle_timeout=60,
                                   max_data=4 * 1024 * 1024, max_stream_data=512 * 1024)
        certificate = ""
        self._active_streams = 0
        self._protocol = None
        self._transport = None
        self._listener = None
        self._server = None
        token = self.assignment["quic_token"]
        self._preface = ("SWARM_RPC_QUIC_V1 " + token + "\n").encode()
        try:
            if is_worker:
                config.private_key, config.certificate = server_identity()
                certificate = config.certificate.public_bytes(serialization.Encoding.PEM).decode()
                self._server = _PeerServer(self, configuration=config, create_protocol=_StreamProtocol,
                    stream_handler=lambda r, w: self._accept_stream(r, w))
                self._transport, _ = await self._loop.create_datagram_endpoint(
                    lambda: self._server, local_addr=(self.assignment["lan_ip"], 0))
            else:
                # Bind before discovering the peer so the worker can open its
                # stateful firewall mapping towards this exact source port.
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    sock.bind((self.assignment["lan_ip"], 0)); sock.setblocking(False)
                    self._pending_socket = sock
                except BaseException:
                    sock.close()
                    raise
            address = (self._transport.get_extra_info("sockname") if is_worker else sock.getsockname())
            publication = {"agent_id": self.agent_id, "cluster_id": self.assignment["cluster_id"],
                           "port": address[1], "certificate_pem": certificate}
            # The primary may still be downloading its GGUF. A listening worker
            # is ready independently; keep its endpoint until cluster teardown.
            until = time.monotonic() + (1800 if is_worker else 120)
            while not self._stopped.is_set():
                reply = await asyncio.to_thread(self.control, dict(publication, stats=dict(self.stats)))
                if is_worker:
                    self._ready.set()
                peers = reply.get("peers", [])
                if peers:
                    peer = peers[0]
                    self.peer_address = (peer["ip"], peer["port"])
                    if not is_worker:
                        config.cadata = peer["certificate_pem"].encode()
                        config.server_name = "swarm-rpc"
                        config.verify_mode = ssl.CERT_REQUIRED
                    break
                if time.monotonic() >= until:
                    raise TimeoutError("QUIC peer endpoint did not register")
                await asyncio.sleep(.5)
            if self._stopped.is_set():
                return
            if not is_worker:
                self._protocol = _PeerClient(QuicConnection(configuration=config), self.peer_address)
                self._transport, _ = await self._loop.create_datagram_endpoint(lambda: self._protocol, sock=sock)
                self._pending_socket = None
                self._protocol.connect(self.peer_address)
            self._spawn(self._maintain(publication))
            if not is_worker:
                await asyncio.wait_for(self._protocol.wait_connected(), 45)
                self.profile = self._protocol.profile
                self.stats["connected"] = True
                self._listener = await asyncio.start_server(self._accept_tcp, "127.0.0.1", 0,
                                                            limit=self._protocol.chunk_size * 2)
                self.endpoint = "127.0.0.1:" + str(self._listener.sockets[0].getsockname()[1])
            self._ready.set()
            await self._stopped.wait()
        finally:
            if self._listener:
                self._listener.close()
                await self._listener.wait_closed()
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
            if self._protocol:
                self._protocol.close()
            if self._server:
                self._server.close()
            if self._transport:
                self._transport.close()
            pending = getattr(self, "_pending_socket", None)
            if pending:
                pending.close()

    async def _maintain(self, publication):
        last_control = last_ping = last_ok = time.monotonic()
        while not self._stopped.is_set():
            self._transport.sendto(PUNCH, self.peer_address)
            now = time.monotonic()
            if now - last_control >= 5:
                try:
                    await asyncio.to_thread(self.control, dict(publication, stats=dict(self.stats)))
                    last_ok = time.monotonic()
                except Exception:
                    if time.monotonic() - last_ok > 45:
                        raise RuntimeError("QUIC cluster membership lease could not be renewed") from None
                last_control = time.monotonic()
            if self._protocol and self.stats["connected"] and now - last_ping >= 10:
                await asyncio.wait_for(self._protocol.ping(), 15)
                last_ping = time.monotonic()
            await asyncio.sleep(.5)

    def _accept_stream(self, reader, writer):
        if self._active_streams >= MAX_STREAMS:
            writer.close()
            return
        self._active_streams += 1
        protocol = writer.transport.protocol
        self.profile = protocol.profile
        self._spawn(self._worker_stream(reader, writer, protocol.chunk_size))

    async def _worker_stream(self, reader, writer, chunk_size):
        tcp_writer = None
        try:
            supplied = await asyncio.wait_for(reader.readexactly(len(self._preface)), 10)
            if not hmac.compare_digest(supplied, self._preface):
                writer.close()
                return
            tcp_reader, tcp_writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.native_port, limit=chunk_size * 2), 10)
            writer.write(b"OK\n")
            self.stats["connected"] = True
            self.stats["streams"] += 1
            await bridge_stream(tcp_reader, tcp_writer, reader, writer, self.stats, chunk_size=chunk_size)
        finally:
            self._active_streams -= 1
            if tcp_writer:
                tcp_writer.close()
            writer.close()

    def _accept_tcp(self, reader, writer):
        if self._active_streams >= MAX_STREAMS:
            writer.close()
            return
        self._active_streams += 1
        self._spawn(self._primary_stream(reader, writer))

    async def _primary_stream(self, reader, writer):
        quic_writer = None
        try:
            qr, quic_writer = await self._protocol.create_stream()
            quic_writer.write(self._preface)
            if await asyncio.wait_for(qr.readexactly(3), 10) != b"OK\n":
                raise RuntimeError("QUIC worker rejected RPC stream authentication")
            self.stats["streams"] += 1
            await bridge_stream(reader, writer, qr, quic_writer, self.stats, chunk_size=self._protocol.chunk_size)
        finally:
            self._active_streams -= 1
            writer.close()
            if quic_writer:
                quic_writer.close()
