"""Verify incremental delivery through real HTTP rather than ASGI buffering."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import requests
import uvicorn
from test_agent_reports import ROOT, load


class StreamingHTTPTests(unittest.TestCase):
    def test_consumer_receives_reasoning_before_agent_commits_final_response(self):
        self.exchange(False)

    def test_http_disconnect_fences_job_and_releases_stream_registry(self):
        self.exchange(True)

    def exchange(self, disconnect):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "ADMIN_KEY": "admin", "AGENT_KEY": "agent", "CONSUMER_KEY": "consumer",
            "RPC_RELAY_URL": "", "RPC_RELAY_ADMIN_KEY": ""}):
            c = load("real_http_stream_coordinator", ROOT / "coordinator/coordinator.py")
            c.DB_PATH = str(Path(folder) / "db.sqlite"); c._init_db()
            for index, name in enumerate(("primary", "worker")):
                reg = c.RegisterPayload(agent_id=name, hostname=name, port=11434, model="qwen3:14b",
                    gpu_vram_gb=12, lan_ip="10.0.0.%s" % (index + 1))
                c.machines[name] = c.MachineState("nat", reg)
                c.machines[name].runtime = {"rpc_quic": True, "rpc_stream": True}
            cluster = c._form_rpc_cluster("qwen3:30b-a3b", 19, 32768, "quic")
            cluster.status = "active"; cluster.primary_ready = True
            listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen()
            base = "http://127.0.0.1:%s" % listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(c.app, log_level="error", lifespan="off"))
            thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
            release = threading.Event(); finalized = threading.Event(); failures = []
            thread.start()
            worker = None
            try:
                until = time.monotonic() + 5
                while not server.started:
                    if time.monotonic() > until:
                        self.fail("HTTP server did not start")
                    time.sleep(.01)
                def run_worker():
                    try:
                        headers = {"X-API-Key": "agent"}
                        until = time.monotonic() + 5
                        while True:
                            response = requests.get(base + "/agent/jobs/next", headers=headers,
                                params={"agent_id": cluster.primary_id, "model": cluster.model}, timeout=2)
                            if response.status_code == 200:
                                job = response.json(); break
                            if time.monotonic() > until:
                                raise RuntimeError("No streaming job was claimed")
                            time.sleep(.01)
                        self.assertTrue(json.loads(job["body_json"])["stream"])
                        prefix = base + "/agent/jobs/" + job["id"]
                        payload = {"agent_id": cluster.primary_id, "claim_token": job["claim_token"],
                            "sequence": 0, "chunks": [{"choices": [{"index": 0,
                            "delta": {"reasoning_content": "working"}}]}]}
                        requests.post(prefix + "/events", json=payload, headers=headers, timeout=2).raise_for_status()
                        if not release.wait(5):
                            raise RuntimeError("Consumer did not receive the partial delta")
                        if disconnect:
                            until = time.monotonic() + 3
                            # Job cancellation can finish just before the SSE
                            # generator closes; wait for both cleanup owners.
                            while (job["id"] in c.jobs or job["id"] in c.active_chat_streams) and time.monotonic() < until:
                                time.sleep(.01)
                            self.assertNotIn(job["id"], c.jobs)
                            self.assertFalse(c.active_chat_streams)
                            self.assertTrue(c.machines[cluster.primary_id].busy)
                            late = requests.post(prefix + "/events", json=dict(payload, sequence=1), headers=headers, timeout=2)
                            self.assertEqual(late.status_code, 410)
                            late = requests.post(prefix + "/result", json={"claim_token": job["claim_token"], "error": "cancelled"}, headers=headers, timeout=2)
                            self.assertEqual(late.status_code, 404)
                            self.assertFalse(c.machines[cluster.primary_id].busy)
                            return
                        payload.update(sequence=1, chunks=[{"choices": [{"index": 0, "delta": {"content": "56"}}]}])
                        requests.post(prefix + "/events", json=payload, headers=headers, timeout=2).raise_for_status()
                        requests.post(prefix + "/result", headers=headers, timeout=2, json={
                            "claim_token": job["claim_token"], "result": {"choices": [{"message": {"content": "56"},
                            "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}}).raise_for_status()
                        finalized.set()
                    except BaseException as exc:
                        failures.append(exc)
                worker = threading.Thread(target=run_worker, daemon=True); worker.start()
                with requests.post(base + "/v1/chat/completions", headers={"X-API-Key": "consumer"},
                    json={"model": cluster.model, "stream": True, "stream_options": {"include_usage": True},
                          "messages": [{"role": "user", "content": "7 times 8"}]}, stream=True, timeout=8) as response:
                    self.assertEqual(response.status_code, 200)
                    lines = response.iter_lines(chunk_size=None)
                    first = next(lines)
                    self.assertIn(b'"reasoning_content": "working"', first)
                    self.assertFalse(finalized.is_set())
                    if disconnect:
                        response.close()
                    release.set()
                    if not disconnect:
                        remaining = b"\n".join(lines)
                        self.assertIn(b'"content": "56"', remaining)
                        self.assertIn(b'"usage"', remaining)
                        self.assertIn(b"[DONE]", remaining)
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
                self.assertFalse(failures, repr(failures))
            finally:
                release.set()
                if worker:
                    worker.join(timeout=6)
                server.should_exit = True; thread.join(timeout=5); listener.close()
                self.assertFalse(thread.is_alive())
