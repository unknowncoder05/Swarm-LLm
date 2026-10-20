"""Failure evidence must survive retries, registration and coordinator restarts."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class AgentReports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = load("report_test_agent", ROOT / "agents" / "agent.py")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.agent._coordinator = "https://coordinator.invalid/api"
        self.agent._api_key = "private-agent-secret"
        self.agent._agent_id = "worker-1"
        self.agent._model = "qwen3:14b"
        self.paths = patch.object(self.agent, "_report_outbox", return_value=Path(self.temp.name))
        self.paths.start()
        self.addCleanup(self.paths.stop)
        home = patch.object(self.agent.Path, "home", return_value=Path(self.temp.name))
        home.start()
        self.addCleanup(home.stop)

    def test_pull_keeps_real_error_exit_code_and_redacts_secret(self):
        process = Mock(stdout=io.StringIO("pulling manifest\nError: disk full private-agent-secret\n"),
                       returncode=1)
        with patch.object(self.agent.subprocess, "Popen", return_value=process):
            with self.assertRaises(RuntimeError) as raised:
                self.agent.pull_model("ollama", "qwen3:14b", 11435)
        self.assertIn("disk full", str(raised.exception))
        self.assertNotIn("private-agent-secret", str(raised.exception))
        self.assertEqual(raised.exception.exit_code, 1)
        self.assertEqual(raised.exception.model, "qwen3:14b")

    def test_windows_cp1252_output_handles_ollama_unicode_spinner(self):
        raw = io.BytesIO()
        output = io.TextIOWrapper(raw, encoding="cp1252")
        process = Mock(stdout=io.StringIO("pulling manifest ⠋\nprogress 100%\nsuccess\n"),
                       returncode=0)
        with patch.object(self.agent.sys, "stdout", output), \
             patch.object(self.agent.sys, "stderr", output), \
             patch.object(self.agent.subprocess, "Popen", return_value=process), \
             patch.object(self.agent, "write_status"):
            self.agent._configure_stdio()
            self.agent.pull_model("ollama", "qwen3:14b", 11434)
        output.flush()
        self.assertIn("pulling manifest ⠋", raw.getvalue().decode("utf-8"))
        self.assertIn("ready", raw.getvalue().decode("utf-8"))

    def test_offline_report_survives_restart_and_replays_same_id(self):
        with patch.object(self.agent.requests, "post", side_effect=OSError("offline")):
            self.agent._report_failure(RuntimeError("disk full"), "startup traceback")
        files = list(Path(self.temp.name).glob("*.json"))
        self.assertEqual(len(files), 1)
        saved = json.loads(files[0].read_text())
        self.assertEqual(saved["phase"], "ERROR")
        self.assertIn("disk_free_gb", saved["diagnostics"])
        # Load a new module to model an actual watchdog restart.
        restarted = load("report_test_restarted", ROOT / "agents" / "agent.py")
        restarted._coordinator, restarted._api_key = self.agent._coordinator, self.agent._api_key
        with patch.object(restarted, "_report_outbox", return_value=Path(self.temp.name)), \
             patch.object(restarted.requests, "post", return_value=Mock()) as post:
            restarted._flush_reports()
        self.assertEqual(post.call_args.kwargs["json"]["report_id"], saved["report_id"])
        self.assertEqual(list(Path(self.temp.name).glob("*.json")), [])

    def test_traceback_and_signed_urls_are_redacted(self):
        with patch.object(self.agent.requests, "post", return_value=Mock()) as post:
            try:
                raise RuntimeError("private-agent-secret https://download.invalid/blob?token=hidden")
            except RuntimeError as exc:
                self.agent._log_crash(exc)
        report = post.call_args.kwargs["json"]
        self.assertIn("test_traceback", report["traceback"])
        self.assertNotIn("private-agent-secret", json.dumps(report))
        self.assertNotIn("token=hidden", json.dumps(report))

    def test_failed_http_delivery_keeps_report(self):
        response = Mock()
        response.raise_for_status.side_effect = RuntimeError("503")
        with patch.object(self.agent.requests, "post", return_value=response):
            self.agent._report_failure(RuntimeError("failed"))
        self.assertEqual(len(list(Path(self.temp.name).glob("*.json"))), 1)

    def test_entrypoint_reports_startup_failure_and_exits_nonzero(self):
        import ast
        source = ast.parse((ROOT / "agents" / "agent.py").read_text())
        entrypoint = source.body[-1]
        main = Mock(side_effect=RuntimeError("startup pull failed"))
        report = Mock()
        namespace = {"__name__": "__main__", "main": main, "_log_crash": report, "sys": sys}
        with self.assertRaises(SystemExit) as raised:
            exec(compile(ast.Module(body=[entrypoint], type_ignores=[]), "agent.py", "exec"), namespace)
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(str(report.call_args.args[0]), "startup pull failed")

    def test_process_lock_excludes_same_port_and_releases_on_close(self):
        root = Path(self.temp.name)
        child_code = (
            "import importlib.util,sys; from pathlib import Path; "
            f"spec=importlib.util.spec_from_file_location('worker',{str(ROOT / 'agents' / 'agent.py')!r}); "
            "worker=importlib.util.module_from_spec(spec); spec.loader.exec_module(worker); "
            f"worker._context_paths=lambda: {{'root':Path({str(root)!r})}}; "
            "sys.exit(0 if worker._acquire_instance_lock(int(sys.argv[1])) else 23)"
        )
        with patch.object(self.agent, "_context_paths", return_value={"root": root}):
            self.assertTrue(self.agent._acquire_instance_lock(11434))
            try:
                same = subprocess.run([sys.executable, "-c", child_code, "11434"], capture_output=True, timeout=15)
                other = subprocess.run([sys.executable, "-c", child_code, "11435"], capture_output=True, timeout=15)
                self.assertEqual(same.returncode, 23, same.stderr)
                self.assertEqual(other.returncode, 0, other.stderr)
            finally:
                self.agent._release_instance_lock()
            restarted = subprocess.run([sys.executable, "-c", child_code, "11434"], capture_output=True, timeout=15)
            self.assertEqual(restarted.returncode, 0, restarted.stderr)

    def test_supervised_update_leaves_restart_to_watchdog(self):
        target = Path(self.temp.name) / "agent.py"
        with patch.object(self.agent, "__file__", str(target)), \
             patch.object(self.agent.platform, "system", return_value="Windows"), \
             patch.dict(os.environ, {"SWARM_LLM_SUPERVISED": "1"}), \
             patch.object(self.agent.subprocess, "Popen") as spawn, \
             patch.object(self.agent.os, "_exit", side_effect=SystemExit) as exit_process:
            with self.assertRaises(SystemExit):
                self.agent._apply_update("# updated agent\n")
        exit_process.assert_called_once_with(75)
        spawn.assert_not_called()
        self.assertEqual(target.read_text(), "# updated agent\n")

    def test_overlapping_update_checks_do_not_fetch_or_spawn(self):
        self.agent._update_lock.acquire()
        try:
            with patch.object(self.agent.requests, "get") as get:
                self.agent._check_for_update(self.agent._coordinator, {})
            get.assert_not_called()
        finally:
            self.agent._update_lock.release()

    def test_healthy_ollama_is_reused_without_killing_it(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.agent.sys, "argv", ["agent.py", "--coordinator",
                "https://coordinator.invalid/api", "--api-key", "private-agent-secret",
                "--model", "qwen3:14b", "--skip-model-pull"]))
            mocks = {}
            for name, value in {
                "get_agent_id": "worker-1", "_load_context": {},
                "detect_hardware": {"vram_gb": 12, "network_mbps": None},
                "_acquire_instance_lock": True, "_save_context": None,
                "write_status": None,
                "_install_windows_watchdog": None, "test_coordinator": True,
                "_flush_reports": None, "_check_for_update": None,
                "find_ollama": "ollama", "wait_ollama_ready": True,
                "measure_network_speed": None, "register": None,
                "start_media_loop": None, "work_loop": None,
            }.items():
                mocks[name] = stack.enter_context(patch.object(self.agent, name, return_value=value))
            spawn = stack.enter_context(patch.object(self.agent.subprocess, "Popen"))
            run = stack.enter_context(patch.object(self.agent.subprocess, "run"))
            check = stack.enter_context(patch.object(self.agent.subprocess, "check_output"))
            stack.enter_context(patch.object(self.agent.threading, "Thread"))
            self.agent.main()
        spawn.assert_not_called()
        run.assert_not_called()
        check.assert_not_called()
        self.assertIsNone(self.agent._ollama_proc)
        mocks["work_loop"].assert_called_once()

    def test_duplicate_agent_returns_before_modifying_context_or_ollama(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.agent.sys, "argv", ["agent.py", "--coordinator",
                "https://coordinator.invalid/api", "--api-key", "private-agent-secret", "--model", "qwen3:14b"]))
            stack.enter_context(patch.object(self.agent, "get_agent_id", return_value="worker-1"))
            stack.enter_context(patch.object(self.agent, "_load_context", return_value={}))
            stack.enter_context(patch.object(self.agent, "detect_hardware", return_value={"vram_gb": 12}))
            stack.enter_context(patch.object(self.agent, "_acquire_instance_lock", return_value=False))
            save = stack.enter_context(patch.object(self.agent, "_save_context"))
            find = stack.enter_context(patch.object(self.agent, "find_ollama"))
            self.agent.main()
        save.assert_not_called()
        find.assert_not_called()

    def test_heartbeat_reports_readiness_and_defers_to_rpc_mode(self):
        with patch.object(self.agent, "wait_ollama_ready", return_value=False) as ready:
            normal = self.agent._heartbeat_payload(11434)
            self.assertFalse(normal["ollama_ready"])
            self.assertEqual(normal["agent_revision"], self.agent._AGENT_REVISION)
            self.agent._rpc_active.set()
            try:
                with patch.object(self.agent.subprocess, "check_output", return_value="8421\n"):
                    rpc = self.agent._heartbeat_payload(11434)
            finally:
                self.agent._rpc_active.clear()
            self.assertIsNone(rpc["ollama_ready"])
            self.assertEqual(rpc["gpu_used_mb"], 8421)
            ready.assert_called_once()

    def test_rpc_resolves_windows_default_store_and_override(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": "unused"}), \
             patch.object(self.agent.platform, "system", return_value="Windows"):
            os.environ.pop("OLLAMA_MODELS", None)
            self.assertEqual(self.agent._ollama_models_dir(), Path(self.temp.name) / ".ollama" / "models")
        with patch.dict(os.environ, {"OLLAMA_MODELS": self.temp.name}):
            self.assertEqual(self.agent._ollama_models_dir(), Path(self.temp.name))

    def test_rpc_primary_pulls_missing_weights_before_stopping_ollama(self):
        with patch.object(self.agent, "resolve_gguf", side_effect=[None, "model.gguf"]), \
             patch.object(self.agent, "pull_model") as pull, \
             patch.object(self.agent, "write_status"), \
             patch.object(self.agent, "ensure_llamacpp", side_effect=RuntimeError("stop at binary install")), \
             patch.object(self.agent, "_stop_ollama_for_rpc") as stop:
            with self.assertRaisesRegex(RuntimeError, "binary install"):
                self.agent.run_rpc_primary({"cluster_id": "test", "model": "qwen3:30b-a3b"}, "unused", "unused")
        pull.assert_called_once_with(self.agent._ollama_exe, "qwen3:30b-a3b", self.agent._ollama_port)
        stop.assert_not_called()

    def test_rpc_failure_keeps_native_log_tail(self):
        log = Path(self.temp.name) / "llama-primary.log"
        log.write_text("CUDA: insufficient memory\n")
        with patch.object(self.agent, "_rpc_log_path", log), \
             patch.object(self.agent.requests, "post", return_value=Mock()) as post:
            self.agent._report_failure(RuntimeError("native exited"))
        self.assertIn("CUDA: insufficient memory", post.call_args.kwargs["json"]["log_tail"])

    def test_rpc_readiness_aborts_immediately_when_native_process_exits(self):
        with patch.object(self.agent.requests, "get") as get:
            self.assertFalse(self.agent._wait_llama_ready(18080, timeout=900, proc=Mock(poll=Mock(return_value=1))))
        get.assert_not_called()

    def test_rpc_primary_uses_os_selected_loopback_port(self):
        from unittest.mock import MagicMock
        probe = MagicMock()
        probe.__enter__.return_value = probe
        probe.getsockname.return_value = ("127.0.0.1", 51234)
        with patch.object(self.agent.socket, "socket", return_value=probe), \
             patch.object(self.agent, "resolve_gguf", return_value="model.gguf"), \
             patch.object(self.agent, "ensure_llamacpp", return_value={"llama_server": "llama-server"}), \
             patch.object(self.agent, "_stop_ollama_for_rpc"), \
             patch.object(self.agent, "_stop_rpc_process"), \
             patch.object(self.agent, "_launch_rpc_process", return_value=Mock(poll=Mock(return_value=1))) as launch, \
             patch.object(self.agent, "_wait_llama_ready", return_value=False) as ready:
            with self.assertRaisesRegex(RuntimeError, "exit 1"):
                self.agent.run_rpc_primary({"cluster_id": "test", "model": "qwen3:30b-a3b",
                    "llama_port": 18080}, "unused", "unused")
        probe.bind.assert_called_once_with(("127.0.0.1", 0))
        command = launch.call_args.args[0]
        self.assertEqual(command[command.index("--port") + 1], "51234")
        self.assertEqual(ready.call_args.args[0], 51234)

    def test_windows_cuda_installer_uses_pinned_archives_without_github_api(self):
        import zipfile
        def archive(files):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as zipped:
                for name in files:
                    zipped.writestr(name, b"fixture")
            return stream.getvalue()
        binaries = archive(["llama-server.exe", "ggml-rpc-server.exe", "ggml-rpc.dll"])
        runtime = archive(["cublas64_12.dll"])
        downloads = []
        def download(url, **kwargs):
            self.assertTrue(url.startswith("https://github.com/ggml-org/llama.cpp/releases/download/b11612/"))
            downloads.append(url)
            return io.BytesIO(runtime if "cudart-" in url else binaries)
        cache = Path(self.temp.name) / "binary-cache"
        with patch.object(self.agent.platform, "system", return_value="Windows"), \
             patch.object(self.agent, "_LLAMACPP_CACHE", cache), \
             patch.object(self.agent, "write_status"), \
             patch.object(self.agent, "urlopen", side_effect=download):
            installed = self.agent.install_llamacpp()
        self.assertEqual(len(downloads), 2)
        self.assertTrue(Path(installed["llama_rpc_server"]).exists())
        self.assertTrue((cache / "cublas64_12.dll").exists())
        self.assertEqual(list(cache.glob("*.zip")), [])


class CoordinatorReports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.TestClient = TestClient
        with patch.dict(os.environ, {"ADMIN_KEY": "report-admin", "AGENT_KEY": "report-agent",
                                     "CONSUMER_KEY": "report-consumer"}):
            cls.coordinator = load("report_test_coordinator", ROOT / "coordinator" / "coordinator.py")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.coordinator.DB_PATH = str(Path(self.temp.name) / "coordinator.db")
        self.coordinator._init_db()
        self.coordinator.machines.clear()
        self.coordinator.pending.clear()
        self.coordinator.rpc_clusters.clear()
        self.coordinator.agent_rpc_assignments.clear()
        # No lifespan: avoid background GitHub/media/RPC tasks in these tests.
        self.client = self.TestClient(self.coordinator.app)
        self.addCleanup(self.client.close)
        self.agent_headers = {"X-API-Key": "report-agent"}
        self.admin_headers = {"X-API-Key": "report-admin"}

    def report(self, **changes):
        payload = {"report_id": "failure-1", "agent_id": "worker-1", "hostname": "windows-1",
                   "model": "qwen3:14b", "phase": "ERROR", "detail": "disk full report-agent",
                   "traceback": "startup traceback", "log_tail": "Ollama: disk full", "exit_code": 1}
        payload.update(changes)
        response = self.client.post("/agent-status", json=payload, headers=self.agent_headers)
        self.assertEqual(response.status_code, 200, response.text)

    def test_reports_survive_progress_registration_and_restart(self):
        self.report()
        self.client.post("/agent-status", json={"agent_id": "worker-1", "phase": "CONNECTING"},
                         headers=self.agent_headers)
        self.client.post("/register", json={"agent_id": "worker-1", "hostname": "windows-1",
                                            "port": 11434, "model": "qwen3:14b"},
                         headers=self.agent_headers)
        machine = self.client.get("/machines", headers=self.admin_headers).json()["machines"][0]
        self.assertEqual(machine["last_error"]["exit_code"], 1)
        self.assertNotIn("report-agent", machine["last_error"]["detail"])
        self.coordinator._init_db()
        self.coordinator.machines.clear()
        self.coordinator.pending.clear()
        reports = self.client.get("/agent-reports?hostname=windows-1", headers=self.admin_headers).json()["reports"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["traceback"], "startup traceback")

    def test_registered_agent_error_is_stored_and_retries_deduplicate(self):
        self.client.post("/register", json={"agent_id": "worker-1", "port": 11434, "model": "qwen3:14b"},
                         headers=self.agent_headers)
        self.report()
        self.report()
        reports = self.client.get("/agent-reports?agent_id=worker-1", headers=self.admin_headers).json()["reports"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(self.coordinator.pending, {})

    def test_reports_are_admin_only_and_submission_requires_agent_key(self):
        self.assertEqual(self.client.get("/agent-reports", headers=self.agent_headers).status_code, 401)
        self.assertEqual(self.client.get("/agent-reports").status_code, 401)
        self.assertEqual(self.client.post("/agent-status", json={"phase": "ERROR"}).status_code, 401)

    def test_large_reports_are_rejected(self):
        response = self.client.post("/agent-status", headers=self.agent_headers,
                                    json={"phase": "ERROR", "log_tail": "x" * 16385})
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/agent-status", headers=self.agent_headers,
                                    json={"phase": "ERROR", "diagnostics": {"oversize": "x" * 65537}})
        self.assertEqual(response.status_code, 413)

    def test_heartbeat_exposes_running_revision_and_ollama_health(self):
        self.client.post("/register", json={"agent_id": "worker-1", "port": 11434, "model": "qwen3:14b"},
                         headers=self.agent_headers)
        response = self.client.post("/heartbeat", json={"agent_id": "worker-1", "port": 11434,
            "agent_pid": 123, "agent_revision": "a" * 64, "ollama_pid": 124, "ollama_ready": False},
            headers=self.agent_headers)
        self.assertEqual(response.status_code, 200)
        machine = self.client.get("/machines", headers=self.admin_headers).json()["machines"][0]
        self.assertEqual(machine["runtime"]["agent_pid"], 123)
        self.assertEqual(machine["runtime"]["agent_revision"], "a" * 64)
        self.assertFalse(machine["runtime"]["ollama_ready"])
        response = self.client.post("/heartbeat", json={"agent_id": "worker-1", "port": 11434},
                                    headers=self.agent_headers)
        self.assertEqual(response.status_code, 200)

    def test_lan_address_survives_coordinator_restart(self):
        self.client.post("/register", json={"agent_id": "worker-1", "port": 11434,
            "model": "qwen3:14b", "gpu_vram_gb": 12, "lan_ip": "10.0.0.153"},
            headers=self.agent_headers)
        self.coordinator._init_db()
        restored = self.coordinator._load_machines()
        self.assertEqual(restored["worker-1"].lan_ip, "10.0.0.153")
        self.assertFalse(restored["worker-1"].alive)

    def test_legacy_gpu_registration_refreshes_missing_lan_on_heartbeat(self):
        self.client.post("/register", json={"agent_id": "worker-1", "port": 11434,
            "model": "qwen3:14b", "gpu_vram_gb": 12}, headers=self.agent_headers)
        heartbeat = {"agent_id": "worker-1", "port": 11434, "agent_pid": 123,
                     "agent_revision": "a" * 64}
        response = self.client.post("/heartbeat", json=heartbeat, headers=self.agent_headers)
        self.assertEqual(response.status_code, 404)
        self.client.post("/register", json={"agent_id": "worker-1", "port": 11434,
            "model": "qwen3:14b", "gpu_vram_gb": 12, "lan_ip": "10.0.0.153"},
            headers=self.agent_headers)
        response = self.client.post("/heartbeat", json=heartbeat, headers=self.agent_headers)
        self.assertEqual(response.status_code, 200)

    def test_rpc_cluster_uses_two_gpus_private_addresses_and_requested_context(self):
        for idx in (1, 2):
            self.client.post("/register", json={"agent_id": f"worker-{idx}", "port": 11434,
                "model": "qwen3:14b", "gpu_vram_gb": 12, "lan_ip": f"10.0.0.{150+idx}"},
                headers=self.agent_headers)
        cluster = self.coordinator._form_rpc_cluster("qwen3:30b-a3b", 19, ctx_size=2048)
        self.assertEqual(cluster.primary_id, "worker-1")
        self.assertEqual(cluster.worker_ids, ["worker-2"])
        primary = self.coordinator.agent_rpc_assignments["worker-1"]
        worker = self.coordinator.agent_rpc_assignments["worker-2"]
        self.assertEqual(primary["workers"], ["10.0.0.152:50052"])
        self.assertEqual(primary["ctx_size"], 2048)
        self.assertEqual(worker["listen_host"], "10.0.0.152")
        self.assertEqual(worker["primary_ip"], "10.0.0.151")

    def test_stale_rpc_done_cannot_remove_a_new_assignment(self):
        self.coordinator.agent_rpc_assignments["worker-1"] = {"cluster_id": "new"}
        response = self.client.post("/agent/rpc/done", json={"agent_id": "worker-1",
            "cluster_id": "old", "role": "primary"}, headers=self.agent_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.coordinator.agent_rpc_assignments["worker-1"]["cluster_id"], "new")

    def test_unassigned_worker_never_gets_same_nat_primary_assignment(self):
        self.client.post("/register", json={"agent_id": "worker-1", "port": 11434, "model": "qwen3:14b"},
                         headers=self.agent_headers)
        self.coordinator.agent_rpc_assignments["worker-1"] = {"cluster_id": "test", "role": "primary"}
        response = self.client.get("/agent/rpc/assignment?agent_id=worker-2", headers=self.agent_headers)
        self.assertEqual(response.status_code, 204)


if __name__ == "__main__":
    unittest.main()
