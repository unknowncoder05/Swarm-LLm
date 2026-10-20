"""Loopback relay endpoints must work without Windows fixed-port privileges."""
from contextlib import ExitStack
import socket
import unittest
from unittest.mock import Mock, patch

from test_agent_reports import ROOT, load


class RpcLocalPortTests(unittest.TestCase):
    def setUp(self):
        self.a = load("local_port_agent", ROOT / "agent/agent.py")
        self.addCleanup(self.a._stop_rpc_relays)

    def test_reserved_coordinator_hint_is_never_bound_and_socket_stays_owned(self):
        a = self.a
        real_socket = socket.socket
        binds = []
        class WindowsSocket(real_socket):
            def bind(self, address):
                binds.append(address)
                if address[1] == 50552:
                    raise PermissionError(10013, "Windows excluded port")
                return super().bind(address)
        with patch.object(a.socket, "socket", WindowsSocket), patch.object(a.threading, "Thread"):
            endpoint = a._start_rpc_relay_listener({}, {"local_port": 50552}, "http://coordinator", "key")
        host, port = endpoint.split(":")
        self.assertEqual(host, "127.0.0.1")
        self.assertGreater(int(port), 0)
        self.assertEqual(binds, [("127.0.0.1", 0)])
        with real_socket() as competing:
            with self.assertRaises(OSError):
                competing.bind((host, int(port)))
        with socket.create_connection((host, int(port)), timeout=1):
            pass
        a._stop_rpc_relays()
        with self.assertRaises(OSError):
            socket.create_connection((host, int(port)), timeout=.1)

    def test_primary_uses_every_actual_bound_endpoint_in_native_command(self):
        a = self.a
        assignment = {"cluster_id": "trial", "model": "qwen3:30b-a3b", "transport": "relay",
                      "workers": ["127.0.0.1:50552", "127.0.0.1:50553"],
                      "relay_workers": [{"agent_id": "one", "local_port": 50552},
                                        {"agent_id": "two", "local_port": 50553}]}
        with ExitStack() as stack:
            for name, value in {"resolve_gguf": "cached.gguf", "ensure_llamacpp": {"llama_server": "native"},
                                "_stop_ollama_for_rpc": None, "_wait_tcp": True}.items():
                stack.enter_context(patch.object(a, name, return_value=value))
            stack.enter_context(patch.object(a.threading, "Thread"))
            launch = stack.enter_context(patch.object(a, "_launch_rpc_process", side_effect=KeyboardInterrupt))
            with self.assertRaises(KeyboardInterrupt):
                a.run_rpc_primary(assignment, "http://coordinator", "key")
        command = launch.call_args.args[0]
        endpoints = command[command.index("--rpc") + 1].split(",")
        self.assertEqual(endpoints, [f"127.0.0.1:{sock.getsockname()[1]}" for sock in a._rpc_relay_listeners])
        self.assertEqual(len(set(endpoints)), 2)
        self.assertEqual(assignment["workers"], ["127.0.0.1:50552", "127.0.0.1:50553"])

    def test_relay_worker_native_wait_and_bridge_share_chosen_port(self):
        a = self.a
        assignment = {"cluster_id": "trial", "model": "qwen3:30b-a3b", "transport": "relay",
                      "rpc_port": 50052, "listen_host": "127.0.0.1"}
        process = Mock(pid=42, poll=Mock(return_value=None))
        with ExitStack() as stack:
            for name, value in {"ensure_llamacpp": {"llama_rpc_server": "native"},
                                "_stop_ollama_for_rpc": None, "_available_loopback_port": 54321,
                                "_launch_rpc_process": process, "write_status": None}.items():
                stack.enter_context(patch.object(a, name, return_value=value))
            ready = stack.enter_context(patch.object(a, "_wait_tcp", return_value=True))
            bridge = stack.enter_context(patch.object(a, "_start_rpc_relay_worker"))
            stack.enter_context(patch.object(a, "_rpc_poll_assignment", side_effect=[
                dict(assignment, relay_connections=["connection"]), dict(assignment, dissolve=True)]))
            stack.enter_context(patch.object(a.time, "sleep"))
            launch = a._launch_rpc_process
            a.run_rpc_worker(assignment, "http://coordinator", "key")
            command = launch.call_args.args[0]
        self.assertEqual(command[command.index("--port") + 1], "54321")
        ready.assert_called_once_with("127.0.0.1", 54321, 30)
        self.assertEqual(bridge.call_args.args[0]["rpc_port"], 54321)
        self.assertEqual(assignment["rpc_port"], 50052)

    def test_direct_worker_keeps_coordinator_advertised_port(self):
        a = self.a
        assignment = {"cluster_id": "trial", "model": "qwen3:30b-a3b", "transport": "direct",
                      "rpc_port": 50052, "listen_host": "10.0.0.2"}
        with ExitStack() as stack:
            for name, value in {"ensure_llamacpp": {"llama_rpc_server": "native"},
                                "_stop_ollama_for_rpc": None, "_wait_tcp": True, "write_status": None,
                                "_rpc_poll_assignment": dict(assignment, dissolve=True)}.items():
                stack.enter_context(patch.object(a, name, return_value=value))
            available = stack.enter_context(patch.object(a, "_available_loopback_port"))
            launch = stack.enter_context(patch.object(a, "_launch_rpc_process", return_value=Mock(pid=42)))
            a.run_rpc_worker(assignment, "http://coordinator", "key")
        available.assert_not_called()
        command = launch.call_args.args[0]
        self.assertEqual(command[command.index("--port") + 1], "50052")
        self.assertEqual(command[command.index("--host") + 1], "10.0.0.2")

    def test_quic_primary_preserves_32k_and_allows_a_full_cold_worker_cache_fill(self):
        a = self.a
        assignment = {"cluster_id": "cold", "model": "qwen3:30b-a3b", "transport": "quic",
                      "ctx_size": 32768, "load_timeout": 900, "workers": ["10.0.0.2:50052"]}
        process = Mock(pid=42, poll=Mock(return_value=None))
        with ExitStack() as stack:
            for name, value in {"resolve_gguf": "cached.gguf", "ensure_llamacpp": {"llama_server": "native"},
                                "_stop_ollama_for_rpc": None, "_available_loopback_port": 54321,
                                "_start_rpc_quic_tunnel": Mock(endpoint="127.0.0.1:54322"), "_wait_tcp": True,
                                "_launch_rpc_process": process, "_wait_llama_ready": True,
                                "write_status": None, "_confirm_rpc_ready": False}.items():
                stack.enter_context(patch.object(a, name, return_value=value))
            a.run_rpc_primary(assignment, "http://coordinator", "key")
            a._wait_llama_ready.assert_called_once_with(54321, 1800, proc=process)
            command = a._launch_rpc_process.call_args.args[0]
        self.assertEqual(command[command.index("--ctx-size")+1], "32768")
        self.assertEqual(command[command.index("--rpc")+1], "127.0.0.1:54322")
