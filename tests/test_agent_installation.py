"""Installation handoff and recovery must preserve one supervised worker."""
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from test_agent_reports import ROOT, load


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.agent = load("installation_agent", ROOT / "agent/agent.py")

    def startup(self, stack, *extra):
        a = self.agent
        stack.enter_context(patch.object(a.sys, "argv", ["agent.py", "--coordinator",
            "https://coordinator.invalid/api/", "--api-key", "test-agent-key",
            "--model", "gemma4:12b", *extra]))
        mocked = {}
        for name, value in {"get_agent_id": "node", "_load_context": {},
            "detect_hardware": {"vram_gb": 12}, "_acquire_instance_lock": True,
            "_save_context": None, "_install_windows_watchdog": None,
            "_release_instance_lock": None, "_launch_windows_watchdog": True,
            "test_coordinator": True, "_flush_reports": None, "_check_for_update": None}.items():
            mocked[name] = stack.enter_context(patch.object(a, name, return_value=value))
        stack.enter_context(patch.object(a.platform, "system", return_value="Windows"))
        return mocked

    def test_explicit_dashboard_install_starts_watchdog_immediately(self):
        with ExitStack() as stack:
            mocked = self.startup(stack)
            find = stack.enter_context(patch.object(self.agent, "find_ollama"))
            self.agent.main()
        mocked["_launch_windows_watchdog"].assert_called_once()
        mocked["_release_instance_lock"].assert_called_once()
        find.assert_not_called()
        self.assertEqual(self.agent._coordinator, "https://coordinator.invalid/api")

    def test_foreground_is_an_explicit_opt_out_from_watchdog_handoff(self):
        with ExitStack() as stack:
            mocked = self.startup(stack, "--foreground")
            stack.enter_context(patch.object(self.agent, "find_ollama", side_effect=RuntimeError("foreground reached")))
            with self.assertRaisesRegex(RuntimeError, "foreground reached"):
                self.agent.main()
        mocked["_launch_windows_watchdog"].assert_not_called()

    def test_watchdog_install_failure_retains_configured_report_identity(self):
        with ExitStack() as stack:
            mocked = self.startup(stack)
            mocked["_install_windows_watchdog"].side_effect = OSError("startup folder denied")
            with self.assertRaisesRegex(OSError, "startup folder denied"):
                self.agent.main()
        self.assertEqual(self.agent._agent_id, "node")
        self.assertEqual(self.agent._api_key, "test-agent-key")
        self.assertEqual(self.agent._ollama_port, 11434)

    def test_work_loop_recovery_does_not_spawn_another_heartbeat(self):
        a = self.agent
        a._rpc_assigned.set()
        with patch.object(a.threading, "Thread") as thread, \
             patch.object(a.subprocess, "check_output", return_value="NAME ID SIZE\n"), \
             patch.object(a, "write_status"), \
             patch.object(a.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                a.work_loop("ollama", "http://coordinator", "test-key", 11434, 12)
        thread.assert_not_called()

    def test_hidden_watchdog_restart_delay_does_not_require_console_input(self):
        a = self.agent
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = {"root": root, "agent": root / "agent.py", "launcher": root / "start.cmd",
                     "log": root / "agent.log", "startup": root / "startup" / "agent.vbs"}
            with patch.object(a.platform, "system", return_value="Windows"), \
                 patch.object(a, "_context_paths", return_value=paths):
                a._install_windows_watchdog(root / "config.json")
            launcher = paths["launcher"].read_text()
            self.assertIn("time.sleep(5)", launcher)
            self.assertNotIn("timeout /t", launcher)
            self.assertIn("--supervised", launcher)

    def test_agent_identity_migrates_and_survives_temp_directory_change(self):
        a = self.agent
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = root / "interactive-temp"
            legacy.mkdir()
            expected = str(uuid.uuid4())
            (legacy / "swarm-agent-id.txt").write_text(expected)
            with patch.object(a, "_context_paths", return_value={"root": root / "durable"}), \
                 patch.object(a.tempfile, "gettempdir", return_value=str(legacy)):
                self.assertEqual(a.get_agent_id(), expected)
            with patch.object(a, "_context_paths", return_value={"root": root / "durable"}), \
                 patch.object(a.tempfile, "gettempdir", return_value=str(root / "hidden-temp")):
                self.assertEqual(a.get_agent_id(), expected)

    def test_active_rpc_survives_poll_blips_but_stops_after_bounded_outage(self):
        a = self.agent
        assignment = {"cluster_id": "trial", "role": "worker"}
        a._rpc_active.set()
        response = Mock(status_code=200, json=Mock(return_value=assignment))
        with patch.object(a.requests, "get", side_effect=[response,
                a.requests.exceptions.Timeout(), a.requests.exceptions.Timeout(), a.requests.exceptions.Timeout()]), \
             patch.object(a.time, "monotonic", side_effect=[100, 219, 220]):
            self.assertEqual(a._rpc_poll_assignment("http://coordinator", "key"), assignment)
            self.assertEqual(a._rpc_poll_assignment("http://coordinator", "key"), assignment)
            self.assertEqual(a._rpc_poll_assignment("http://coordinator", "key"), assignment)
            with self.assertRaisesRegex(RuntimeError, "120 seconds"):
                a._rpc_poll_assignment("http://coordinator", "key")

    def test_explicit_unassignment_is_not_hidden_by_cached_role(self):
        a = self.agent
        a._rpc_active.set()
        a._rpc_assignment_cache = {"cluster_id": "old"}
        with patch.object(a.requests, "get", return_value=Mock(status_code=204)):
            self.assertIsNone(a._rpc_poll_assignment("http://coordinator", "key"))
        self.assertIsNone(a._rpc_assignment_cache)

    def test_loaded_model_telemetry_retains_actual_gpu_allocation(self):
        a = self.agent
        models = [{"name": "gemma4:12b", "size": 8000, "size_vram": 7990,
                   "context_length": 2048, "unrelated_field": "omit"}]
        with patch.object(a, "wait_ollama_ready", return_value=True), \
             patch.object(a.requests, "get", return_value=Mock(json=Mock(return_value={"models": models}))):
            payload = a._heartbeat_payload(11434)
        self.assertEqual(payload["ollama_models"][0]["size_vram"], 7990)
        self.assertNotIn("unrelated_field", payload["ollama_models"][0])

    def test_server_version_does_not_depend_on_ollama_being_on_path(self):
        a = self.agent
        with patch.object(a.requests, "get", return_value=Mock(json=Mock(return_value={"version": "1.2.3"}))) as get:
            info = a.get_system_info({})
        self.assertEqual(info["ollama_version"], "1.2.3")
        self.assertTrue(get.call_args.args[0].endswith("/api/version"))

    def test_prior_rpc_log_is_available_after_restart_and_redacted(self):
        a = self.agent
        a._api_key = "private-agent-key"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".swarm").mkdir()
            (root / ".swarm" / "llama-primary.log").write_text("X" * 5000 + "\nCUDA warmup private-agent-key\n")
            with patch.object(a.Path, "home", return_value=root):
                snapshot = a._last_rpc_log()
            self.assertFalse(snapshot["process_running"])
            self.assertEqual(snapshot["role"], "primary")
            self.assertIn("CUDA warmup [redacted]", snapshot["tail"])
            self.assertLessEqual(len(snapshot["tail"]), 4096)

    def test_rpc_log_is_absent_on_a_clean_install(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(self.agent.Path, "home", return_value=Path(temp)):
            self.assertIsNone(self.agent._last_rpc_log())

    def test_worker_exit_after_readiness_raises_with_native_exit_code(self):
        a = self.agent
        assignment = {"cluster_id": "trial", "model": "qwen3:30b-a3b", "rpc_port": 50052}
        process = Mock(pid=42, returncode=7, poll=Mock(return_value=7))
        with patch.object(a, "ensure_llamacpp", return_value={"llama_rpc_server": "rpc"}), \
             patch.object(a, "_stop_ollama_for_rpc"), \
             patch.object(a, "_launch_rpc_process", return_value=process), \
             patch.object(a, "_wait_tcp", return_value=True), \
             patch.object(a, "write_status"), \
             patch.object(a, "_rpc_poll_assignment", return_value=assignment), \
             patch.object(a, "get_local_ip", return_value="127.0.0.1"):
            with self.assertRaisesRegex(RuntimeError, "exit 7") as raised:
                a.run_rpc_worker(assignment, "http://coordinator", "key")
        self.assertEqual(raised.exception.exit_code, 7)

    def test_expected_dissolve_closes_bridges_before_native_socket_reset(self):
        a = self.agent
        for role in ("primary", "worker"):
            with self.subTest(role=role), ExitStack() as stack:
                bridges_closed = []
                process = Mock(pid=42, poll=Mock(return_value=None))
                # Terminating the Windows native process resets its sockets.
                # Bridges must already know this closure was intentional.
                process.terminate.side_effect = lambda: self.assertEqual(bridges_closed, [True])
                def launch(*args):
                    a._rpc_proc = process
                    return process
                values = {"resolve_gguf": "cached-model.gguf", "ensure_llamacpp": {
                    "llama_server": "server", "llama_rpc_server": "worker"},
                    "_stop_ollama_for_rpc": None,
                    "_wait_tcp": True, "_wait_llama_ready": True, "write_status": None,
                    "_restart_ollama_after_rpc": None}
                for name, value in values.items():
                    stack.enter_context(patch.object(a, name, return_value=value))
                stack.enter_context(patch.object(a, "_stop_rpc_relays", side_effect=lambda: bridges_closed.append(True)))
                stack.enter_context(patch.object(a, "_launch_rpc_process", side_effect=launch))
                stack.enter_context(patch.object(a, "_rpc_poll_assignment", side_effect=[
                    {"cluster_id": "trial", "role": role, "model": "qwen3:30b-a3b"},
                    {"cluster_id": "trial", "dissolve": True}, KeyboardInterrupt]))
                stack.enter_context(patch.object(a, "_post_agent_receipt"))
                stack.enter_context(patch.object(a.time, "sleep"))
                stack.enter_context(patch.object(a.requests, "post"))
                with self.assertRaises(KeyboardInterrupt):
                    a._rpc_thread_func("http://coordinator", "key")
                process.terminate.assert_called_once()



class UpdateSourceTests(unittest.TestCase):
    def test_authenticated_update_route_serves_local_reviewed_file_and_fails_closed(self):
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,
                {"ADMIN_KEY": "admin", "AGENT_KEY": "agent", "CONSUMER_KEY": "consumer"}):
            source = Path(temp) / "agent.py"
            source.write_text("# reviewed agent\n", encoding="utf-8")
            c = load("installation_coordinator", ROOT / "coordinator/coordinator.py")
            c._latest_agent_py = "# older upstream version\n"
            client = TestClient(c.app)
            self.addCleanup(client.close)
            with patch.dict(os.environ, {"AGENT_SOURCE_PATH": str(source)}):
                headers = {"X-API-Key": "agent"}
                self.assertEqual(client.get("/agent/agent.py", headers=headers).text, source.read_text())
                self.assertEqual(client.get("/agent/agent.py").status_code, 401)
                source.unlink()
                self.assertEqual(client.get("/agent/agent.py", headers=headers).status_code, 503)


class TrialSelectionTests(unittest.TestCase):
    def test_trial_accepts_new_hostnames_and_excludes_busy_or_offline_nodes(self):
        runner = load("installation_trial", ROOT / "scripts/rpc-trial.py")
        nodes = [{"hostname": name, "alive": True, "gpu_vram_gb": 12} for name in ("new-12", "new-13")]
        nodes += [{"hostname": "old", "alive": False, "gpu_vram_gb": 12},
                  {"hostname": "busy", "alive": True, "busy": True, "gpu_vram_gb": 24}]
        self.assertEqual([m["hostname"] for m in runner.select_trial_machines({"machines": nodes})], ["new-12", "new-13"])

    def test_explicit_pair_does_not_silently_allow_other_eligible_gpu(self):
        runner = load("installation_trial_selection", ROOT / "scripts/rpc-trial.py")
        nodes = [{"hostname": name, "alive": True, "gpu_vram_gb": 12} for name in ("a", "b", "c")]
        self.assertEqual(runner.select_trial_machines({"machines": nodes}, ["a", "b"]), [])


if __name__ == "__main__":
    unittest.main()
