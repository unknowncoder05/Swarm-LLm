"""UDP diagnostics verify real peers without claiming an inference route."""
import copy
import socket
import threading
import time
import unittest
import uuid
from contextlib import ExitStack
from unittest.mock import patch
from test_agent_reports import ROOT, load


class UDPConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.agent = load("udp_connectivity_agent", ROOT / "agent/agent.py")
        self.plan = {"run_id": str(uuid.uuid4()), "token": "a" * 32, "transport": "udp",
                     "created_at": time.time(), "expires_at": time.time() + 10,
                     "agents": [{"agent_id": "one", "ip": "10.8.16.113"},
                                {"agent_id": "two", "ip": "10.8.16.112"}]}

    def test_transport_validation_preserves_tcp_default_and_rejects_unknown_modes(self):
        self.agent._validate_lan_probe(self.plan, "one")
        self.agent._validate_lan_probe({k: v for k, v in self.plan.items() if k != "transport"}, "one")
        with self.assertRaises(ValueError):
            self.agent._validate_lan_probe(dict(self.plan, transport="shell"), "one")

    def test_real_udp_peers_send_and_receive_on_their_advertised_sockets_then_close(self):
        agents = [self.agent, load("udp_connectivity_peer", ROOT / "agent/agent.py")]
        captured = {}; results = {}; errors = []; lock = threading.Lock()
        def publish(identity, result):
            with lock:
                captured[identity] = copy.deepcopy(result)
        def manifest():
            with lock:
                if len(captured) == 2 and all(r["stage"] == "complete" for r in captured.values()):
                    return None
                plan = copy.deepcopy(self.plan)
                plan["endpoints"] = {k: {"ip": "127.0.0.1", "port": r["port"]}
                                     for k, r in captured.items() if r.get("port")}
                return plan
        def run(agent, identity):
            other = "two" if identity == "one" else "one"
            try:
                with ExitStack() as patches:
                    patches.enter_context(patch.object(agent, "_validate_lan_probe", return_value=(
                        {"agent_id": identity, "ip": "127.0.0.1"}, {"agent_id": other, "ip": "127.0.0.1"})))
                    patches.enter_context(patch.object(agent, "_lan_probe_manifest", side_effect=lambda _: manifest()))
                    patches.enter_context(patch.object(agent, "_lan_probe_status", side_effect=lambda r: publish(identity, r)))
                    patches.enter_context(patch.object(agent, "_LAN_UDP_PROBE_SECONDS", 1))
                    patches.enter_context(patch.object(agent, "_LAN_UDP_POLL_SECONDS", .02))
                    results[identity] = agent._run_lan_probe(self.plan, "http://coordinator", identity)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(agent, name)) for agent, name in zip(agents, ("one", "two"))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(12)
        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(set(results), {"one", "two"})
        for result in results.values():
            self.assertTrue(result["passed"], result)
            self.assertGreater(result["received_probes"], 0)
            self.assertGreater(result["received_acks"], 0)
            self.assertLessEqual(result["sent_probes"], 24)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reopened:
                reopened.bind(("127.0.0.1", result["port"]))

    def run_single_peer(self, peer, probe_seconds=.4):
        latest = {}
        def publish(row):
            latest.update(copy.deepcopy(row))
        def manifest(_):
            if latest.get("stage") == "complete":
                return None
            return dict(self.plan, endpoints={"two": {"ip": "127.0.0.1", "port": peer.getsockname()[1]}})
        with patch.object(self.agent, "_validate_lan_probe", return_value=(
                {"agent_id": "one", "ip": "127.0.0.1"}, {"agent_id": "two", "ip": "127.0.0.1"})), \
             patch.object(self.agent, "_lan_probe_manifest", side_effect=manifest), \
             patch.object(self.agent, "_lan_probe_status", side_effect=publish), \
             patch.object(self.agent, "_LAN_UDP_PROBE_SECONDS", probe_seconds), \
             patch.object(self.agent, "_LAN_UDP_POLL_SECONDS", .02), \
             patch.object(self.agent, "_LAN_UDP_MAX_PINGS", 1):
            return self.agent._run_lan_probe(self.plan, "http://coordinator", "one")

    def test_udp_rejects_wrong_source_token_challenge_and_oversized_packets(self):
        errors = []; sender_addresses = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer, \
             socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as impostor:
            peer.bind(("127.0.0.1", 0)); peer.settimeout(3)
            impostor.bind(("127.0.0.1", 0))
            def respond():
                try:
                    packet, address = peer.recvfrom(128); sender_addresses.append(address)
                    reply = packet.replace(b"_PING ", b"_ACK ", 1)
                    impostor.sendto(reply, address)
                    peer.sendto(reply.replace(b"a" * 32, b"b" * 32, 1), address)
                    peer.sendto(reply.rsplit(b" ", 1)[0] + b" " + b"0" * 32 + b"\n", address)
                    peer.sendto(reply + b"x" * 129, address)
                    peer.sendto(reply, address)
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=respond); thread.start()
            result = self.run_single_peer(peer)
            thread.join(4)
        self.assertFalse(errors)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["received_acks"], 1)
        self.assertGreaterEqual(result["ignored_datagrams"], 4)
        self.assertEqual(sender_addresses[0][1], result["port"])

    def test_silent_udp_peer_is_diagnostic_failure_and_closes_socket(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.bind(("127.0.0.1", 0))
            with patch.object(self.agent, "_log_crash") as crash:
                result = self.run_single_peer(peer, probe_seconds=.2)
            crash.assert_not_called()
        self.assertFalse(result["passed"])
        self.assertEqual(result["sent_probes"], 1)
        self.assertEqual(result["received_acks"], 0)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reopened:
            reopened.bind(("127.0.0.1", result["port"]))

    def test_udp_success_does_not_advertise_tcp_relay_candidates(self):
        controller = load("udp_probe_controller", ROOT / "scripts/agent-lan-probe.py")
        rows = [{"peer_id": "one", "passed": True}, {"peer_id": "two", "passed": True}]
        udp = controller.summarize_results(rows, "udp")
        self.assertEqual(udp["status"], "passed")
        self.assertTrue(udp["direct_udp_verified"])
        self.assertEqual(udp["relay_candidate_ids"], [])
        self.assertEqual(controller.summarize_results(rows, "tcp")["relay_candidate_ids"], ["one", "two"])
        rows[1]["passed"] = False
        self.assertEqual(controller.summarize_results(rows, "udp")["status"], "partial")
        self.assertFalse(controller.summarize_results(rows, "udp")["direct_udp_verified"])

    def test_socket_denial_is_saved_as_diagnostic_failure(self):
        with patch.object(self.agent.socket, "socket", side_effect=PermissionError("socket denied")), \
             patch.object(self.agent, "_lan_probe_status") as publish, \
             patch.object(self.agent, "_log_crash") as crash:
            result = self.agent._run_lan_probe(self.plan, "http://coordinator", "one")
        self.assertFalse(result["passed"])
        self.assertIn("socket denied", result["error"])
        publish.assert_called_once_with(result)
        crash.assert_not_called()
