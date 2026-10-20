"""Deployment handoff is one-use, short lived, atomic and bound to live peers."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from test_agent_reports import ROOT, load


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.module = load("rpc_handoff_test", ROOT / "coordinator/rpc_handoff.py")
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "handoff.json"
        self.now = time.time(); self.machines = {}
        self.data = {"version": 1, "created_at": self.now, "clusters": []}
        state = {"cluster_id": "cluster", "model": "qwen3:30b-a3b", "primary_id": "primary", "worker_ids": ["worker"],
                 "transport": "quic", "status": "active", "primary_ready": True, "ctx_size": 32768,
                 "formed_at": self.now - 120, "last_job_at": self.now - 10}
        item = {"cluster": state, "machines": {}, "assignments": {}}
        for index, identity in enumerate(("primary", "worker")):
            ip = f"10.0.0.{index+1}"
            self.machines[identity] = SimpleNamespace(lan_ip=ip, in_rpc=False, last_heartbeat=0)
            item["machines"][identity] = {"lan_ip": ip, "last_heartbeat": self.now-2,
                                          "runtime": {"agent_pid": 123+index, "rpc_pid": 234+index}}
            item["assignments"][identity] = {"cluster_id": "cluster", "model": state["model"], "transport": "quic",
                "role": identity, "lan_ip": ip, "quic_token": "a"*64,
                "quic_endpoint": {"agent_id": identity, "ip": ip, "port": 12345}}
        self.data["clusters"].append(item)

    def save(self, data):
        self.path.write_text(json.dumps(data)); self.path.chmod(0o600)

    def restore(self, clusters, assignments):
        def factory(cid, model, primary, workers, **kwargs):
            return SimpleNamespace(cluster_id=cid, model=model, primary_id=primary, worker_ids=workers, **kwargs)
        return self.module.restore_quic_handoff(self.path,self.machines,clusters,assignments,factory,now=self.now)

    def test_active_native_identity_reservations_and_tokens_survive_once(self):
        self.save(self.data); clusters={}; assignments={}
        self.assertEqual(self.restore(clusters,assignments), ["cluster"])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.restore(clusters,assignments), [])
        self.assertTrue(all(m.in_rpc for m in self.machines.values()))
        self.assertEqual(self.machines["primary"].runtime["rpc_pid"],234)
        self.assertEqual(assignments["primary"]["quic_token"],"a"*64)

    def test_nondefault_native_profile_is_preserved_and_missing_assignment_settings_rejected(self):
        item = self.data["clusters"][0]
        profile = {"batch_size": 128, "ubatch_size": 64, "cache_ram": 0}
        item["cluster"]["runtime_profile"] = profile
        self.save(self.data)
        with self.assertRaises(ValueError):
            self.restore({}, {})
        item["assignments"]["primary"]["runtime_profile"] = profile
        self.save(self.data); clusters = {}; assignments = {}
        self.restore(clusters, assignments)
        self.assertEqual(clusters["cluster"].runtime_profile, profile)

    def test_stale_changed_overlapping_and_http_relay_state_is_rejected_atomically(self):
        mutations = [lambda d:d.update(created_at=self.now-31),
                     lambda d:d["clusters"][0]["cluster"].update(transport="relay"),
                     lambda d:d["clusters"][0]["machines"]["worker"].update(lan_ip="10.0.0.99"),
                     lambda d:d["clusters"][0]["assignments"]["worker"].update(quic_token="b"*64),
                     lambda d:d["clusters"].append(copy.deepcopy(d["clusters"][0]))]
        for mutate in mutations:
            data=copy.deepcopy(self.data);mutate(data);self.save(data);clusters={};assignments={}
            with self.assertRaises(ValueError):self.restore(clusters,assignments)
            self.assertFalse(clusters);self.assertFalse(assignments)
            self.assertFalse(self.path.exists())
            self.assertFalse(any(m.in_rpc for m in self.machines.values()))
