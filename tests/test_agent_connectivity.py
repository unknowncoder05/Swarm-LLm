"""Bounded LAN probes must identify peers, close sockets and preserve GPU work."""
import copy
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from test_agent_reports import ROOT, load


class ConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.a = load("connectivity_agent", ROOT / "agents/agent.py")
        self.plan = {"run_id": str(uuid.uuid4()), "token": "a" * 32,
                     "created_at": time.time(), "expires_at": time.time() + 10,
                     "agents": [{"agent_id": "one", "ip": "10.0.0.113"},
                                {"agent_id": "two", "ip": "10.0.0.112"}]}

    def test_manifest_rejects_expired_unselected_public_and_unbounded_targets(self):
        own, peer = self.a._validate_lan_probe(self.plan, "one")
        self.assertEqual(peer["agent_id"], "two")
        for mutate in (lambda p: p.update(expires_at=time.time() - 1),
                       lambda p: p.update(expires_at=time.time() + 3600),
                       lambda p: p["agents"][1].update(ip="8.8.8.8"),
                       lambda p: p["agents"][1].update(ip="127.0.0.1"),
                       lambda p: p["agents"][1].update(agent_id="one")):
            plan = copy.deepcopy(self.plan); mutate(plan)
            with self.assertRaises(ValueError):
                self.a._validate_lan_probe(plan, "one")
        with self.assertRaises(ValueError):
            self.a._validate_lan_probe(self.plan, "other")

    def test_two_real_listeners_connect_both_directions_then_close(self):
        agents = [self.a, load("connectivity_peer", ROOT / "agents/agent.py")]
        lock = threading.Lock(); results = {}; errors = []; captured = {}
        def publish(identity, result):
            with lock:
                captured[identity] = copy.deepcopy(result)
        def manifest():
            with lock:
                if len(captured) == 2 and all(r["stage"] == "complete" for r in captured.values()):
                    return None
                p = copy.deepcopy(self.plan)
                p["endpoints"] = {k: {"ip": "127.0.0.1", "port": v["port"]}
                                  for k, v in captured.items() if v.get("port")}
                return p
        def run(agent, identity):
            other = "two" if identity == "one" else "one"
            try:
                with patch.object(agent, "_validate_lan_probe", return_value=(
                        {"agent_id": identity, "ip": "127.0.0.1"}, {"agent_id": other, "ip": "127.0.0.1"})), \
                     patch.object(agent, "_lan_probe_manifest", side_effect=lambda _: manifest()), \
                     patch.object(agent, "_lan_probe_status", side_effect=lambda r: publish(identity, r)):
                    results[identity] = agent._run_lan_probe(self.plan, "http://coordinator", identity)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(a, identity)) for a, identity in zip(agents, ("one", "two"))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(12)
        self.assertFalse(errors)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(set(results), {"one", "two"})
        self.assertTrue(all(r["passed"] for r in results.values()))
        for row in results.values():
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", row["port"]), timeout=.2)

    def test_open_tcp_port_without_matching_handshake_is_not_success(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0)); listener.listen(1)
            def wrong_peer():
                peer, _ = listener.accept()
                with peer:
                    peer.recv(128); peer.sendall(b"wrong service\n")
            worker = threading.Thread(target=wrong_peer); worker.start()
            result = self.a._lan_probe_connect("127.0.0.1", listener.getsockname()[1], self.plan["token"])
            worker.join(2)
        self.assertFalse(result["passed"])
        self.assertIn("handshake", result["error"])

    def test_refused_connection_is_diagnostic_result(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
        result = self.a._lan_probe_connect("127.0.0.1", port, self.plan["token"], timeout=.2)
        self.assertFalse(result["passed"])
        self.assertIn("error", result)

    def test_probe_evidence_is_saved_without_overwriting_running_rpc_status(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(self.a, "_context_paths", return_value={"root": Path(temp)}), \
             patch.object(self.a, "write_status") as status:
            self.a._rpc_active.set()
            self.a._lan_probe_status({"run_id": self.plan["run_id"], "stage": "complete", "passed": False})
            status.assert_not_called()
            self.assertTrue((Path(temp) / "lan-probe-result.json").exists())

    def test_probe_holds_off_updater_without_claiming_gpu_work(self):
        self.a._lan_probe_active.set()
        with patch.object(self.a.requests, "get") as get:
            self.a._check_for_update("http://coordinator", {})
        get.assert_not_called()
        self.assertEqual(self.a._chat_claim, (None, None))
        self.assertFalse(self.a._rpc_assigned.is_set())

    def test_non_streaming_rpc_inference_stops_when_consumer_cancels(self):
        assignment = {"cluster_id": "cluster-1"}
        cancelled = unittest.mock.Mock()
        cancelled.raise_for_status.return_value = None
        cancelled.json.return_value = {"cancelled": True}
        self.a._chat_claim = ("job-1", "claim-1")
        with patch.object(self.a, "rpc_infer", side_effect=lambda *_args, **_kwargs: time.sleep(30)), \
             patch.object(self.a, "_check_rpc_quic"), \
             patch.object(self.a, "_rpc_poll_assignment", return_value=assignment), \
             patch.object(self.a.requests, "get", return_value=cancelled) as get:
            with self.assertRaisesRegex(self.a.RpcStreamCancelled, "cancelled native inference"):
                self.a._rpc_infer_with_health({}, 18080, assignment, "https://coordinator", "agent-key")
        get.assert_called_once()
        self.assertIn("/agent/jobs/job-1/cancelled", get.call_args.args[0])

    def test_restarted_agent_acknowledges_assignment_already_dissolving(self):
        assignment = {"cluster_id": "cluster-1", "role": "worker", "dissolve": True}
        with patch.object(self.a, "_rpc_poll_assignment", return_value=assignment), \
             patch.object(self.a, "_post_agent_receipt", side_effect=RuntimeError("stop")) as receipt:
            with self.assertRaisesRegex(RuntimeError, "stop"):
                self.a._rpc_thread_func("https://coordinator", "agent-key")
        receipt.assert_called_once()
        self.assertEqual(receipt.call_args.kwargs["payload"], {
            "agent_id": self.a._agent_id, "cluster_id": "cluster-1", "role": "worker"})
