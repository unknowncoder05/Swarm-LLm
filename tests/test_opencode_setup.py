"""Verify client setup preserves settings and fails safely on auth/install errors."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from test_agent_reports import ROOT, load


class OpenCodeSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.setup = load("opencode_setup_under_test", ROOT / "scripts/setup-opencode.py")
        cls.config = load("opencode_setup_config", ROOT / "scripts/gen_opencode_config.py")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = io.StringIO()
        self.launcher = Mock()
        self.launcher.find_executable.return_value = "/fake/opencode"

    def run_setup(self, argv=(), **extra):
        with patch.object(self.setup, "ROOT", self.root), \
             patch.object(self.setup, "load_launcher", return_value=self.launcher), \
             patch.dict(os.environ, {"SWARM_CONSUMER_KEY": "test-secret", "SWARM_BASE_URL": "https://swarm.example/v1"}, clear=True), \
             patch.object(self.setup.subprocess, "run", return_value=Mock(stdout="1.18.32\n")) as run, \
             contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            if extra.get("missing_key"):
                os.environ.clear()
                os.environ["SWARM_BASE_URL"] = "https://swarm.example/v1"
            return self.setup.main(list(argv)), run

    def test_merge_is_repeatable_preserves_settings_and_unhides_swarm(self):
        path = self.root / "opencode.json"
        original = {"permission": {"bash": "ask"}, "provider": {"other": {"name": "Keep me"},
                    "swarm-llm": {"models": {"custom": {"name": "Keep too"}}}},
                    "enabled_providers": ["other"], "disabled_providers": ["swarm-llm", "blocked"]}
        path.write_text(json.dumps(original))
        for _ in range(2):
            self.run_setup(["--offline"])
        config = json.loads(path.read_text())
        self.assertEqual(config["permission"], original["permission"])
        self.assertEqual(config["provider"]["other"], original["provider"]["other"])
        self.assertIn("custom", config["provider"]["swarm-llm"]["models"])
        self.assertEqual(config["enabled_providers"], ["other", "swarm-llm"])
        self.assertEqual(config["disabled_providers"], ["blocked"])
        self.assertEqual(config["model"], "swarm-llm/qwen3:30b-a3b")
        self.assertNotIn("test-secret", path.read_text() + self.output.getvalue())
        self.assertNotIn("https://swarm.example/v1", path.read_text())
        self.assertEqual(config["provider"]["swarm-llm"]["options"]["baseURL"], "{env:SWARM_BASE_URL}")
        self.assertEqual(self.config.load_base_url(self.root, {}), "https://swarm.example/v1")

    def test_environment_client_file_and_server_file_precedence(self):
        (self.root / ".env").write_text('CONSUMER_KEY="server"\nUNRELATED="C:\\Windows"\n')
        (self.root / ".env.opencode").write_text('\ufeffSWARM_CONSUMER_KEY="client"\n', encoding="utf-8")
        self.assertEqual(self.config.load_consumer_key(self.root, {}), "client")
        self.assertEqual(self.config.load_consumer_key(self.root, {"CONSUMER_KEY": "env"}), "env")
        (self.root / ".env.opencode").unlink()
        self.assertEqual(self.config.load_consumer_key(self.root, {}), "server")
        (self.root / ".env").write_text('SWARM_CONSUMER_KEY="server-swarm"\n')
        self.assertEqual(self.config.load_consumer_key(self.root, {}), "server-swarm")

    def test_launcher_reads_swarm_named_credential_from_env_file(self):
        launcher = load("opencode_setup_launcher", ROOT / "scripts/run-opencode.py")
        (self.root / ".env").write_text("SWARM_CONSUMER_KEY=client-secret\n")
        with patch.object(launcher, "ROOT", self.root), patch.dict(os.environ, {}, clear=True), \
             patch.object(launcher, "find_executable", return_value="/fake/opencode"), \
             patch.object(launcher.os, "execve") as execute:
            launcher.main()
        self.assertEqual(execute.call_args.args[2]["SWARM_CONSUMER_KEY"], "client-secret")

    def test_auth_failure_does_not_install_or_change_config(self):
        path = self.root / "opencode.json"
        path.write_text('{"model": "other/model"}\n')
        with patch.object(self.setup, "check_connection", side_effect=ValueError("rejected")), \
             patch.object(self.setup.shutil, "which") as find_npm:
            with self.assertRaises(SystemExit):
                self.run_setup(["--install"])
        self.assertEqual(path.read_text(), '{"model": "other/model"}\n')
        find_npm.assert_not_called()
        self.assertFalse((self.root / ".env.opencode").exists())

    def test_malformed_config_and_jsonc_are_not_overwritten(self):
        path = self.root / "opencode.json"
        for content in ('{ // comments\n}', '[]', '{"provider": []}'):
            path.write_text(content)
            with self.assertRaises(SystemExit):
                self.run_setup(["--offline"])
            self.assertEqual(path.read_text(), content)
        path.unlink()
        (self.root / "opencode.jsonc").write_text("{}")
        with self.assertRaises(SystemExit):
            self.run_setup(["--offline"])
        self.assertFalse(path.exists())

    def test_no_silent_model_downgrade_when_catalog_is_empty(self):
        with patch.object(self.setup, "check_connection", return_value=[]):
            self.run_setup()
        config = json.loads((self.root / "opencode.json").read_text())
        self.assertEqual(config["model"], "swarm-llm/qwen3:30b-a3b")
        self.assertIn("not currently advertised", self.output.getvalue())

    def test_prompted_key_is_saved_privately_and_reused_without_server_credentials(self):
        with patch.object(self.setup.sys.stdin, "isatty", return_value=True), \
             patch.object(self.setup.getpass, "getpass", return_value="prompt-secret"):
            self.run_setup(["--offline"], missing_key=True)
        keyfile = self.root / ".env.opencode"
        self.assertEqual(self.config.load_consumer_key(self.root, {}), "prompt-secret")
        self.assertNotIn("prompt-secret", self.output.getvalue() + (self.root / "opencode.json").read_text())
        if os.name != "nt":
            self.assertEqual(keyfile.stat().st_mode & 0o777, 0o600)

    def test_noninteractive_missing_key_does_not_write(self):
        with patch.object(self.setup.sys.stdin, "isatty", return_value=False):
            with self.assertRaises(SystemExit):
                self.run_setup(["--offline"], missing_key=True)
        self.assertFalse((self.root / "opencode.json").exists())

    def test_install_uses_repo_local_prefix_and_pinned_cli(self):
        with patch.object(self.setup.shutil, "which", return_value="/fake/npm"):
            _, run = self.run_setup(["--install", "--offline"])
        install = run.call_args_list[0].args[0]
        self.assertEqual(install[:4], ["/fake/npm", "install", "--prefix", str(self.root / ".opencode-tools")])
        self.assertIn("opencode-ai@1.18.32", install)
        self.assertNotIn("-g", install)

    def test_local_cli_is_preferred_without_changing_path(self):
        launcher = load("opencode_local_launcher", ROOT / "scripts/run-opencode.py")
        native = self.root / ".opencode-tools/node_modules/opencode-ai/bin/opencode.exe"
        native.parent.mkdir(parents=True)
        native.write_bytes(b"test")
        with patch.object(launcher.shutil, "which", return_value="/global/opencode"):
            self.assertEqual(launcher.find_executable(self.root), str(native))

    def test_discovered_models_are_printed_and_visible_without_static_ghost_entries(self):
        path = self.root / "opencode.json"
        path.write_text(json.dumps({"provider": {"swarm-llm": {"models": {
            "old-model": {"name": "Preserve override"},
            "qwen3.6:27b": {"limit": {"context": 8192, "output": 4096}}}}}}))
        advertised = ["qwen3:30b-a3b", "qwen3.6:27b", "gemma4:12b"]
        with patch.object(self.setup, "check_connection", return_value=advertised):
            self.run_setup(["--model", "qwen3.6:27b"])
        config = json.loads(path.read_text())
        provider = config["provider"]["swarm-llm"]
        self.assertEqual(provider["whitelist"], advertised)
        self.assertIn("old-model", provider["models"])
        self.assertNotIn("devstral:24b", provider["models"])
        self.assertEqual(provider["models"]["qwen3.6:27b"]["limit"]["context"], 8192)
        self.assertTrue(provider["models"]["qwen3.6:27b"]["tool_call"])
        self.assertEqual(config["small_model"], config["model"])
        for name in advertised:
            self.assertIn(name, self.output.getvalue())

    def test_list_only_is_read_only_and_does_not_require_an_installed_client(self):
        with patch.object(self.setup, "check_connection", return_value=["qwen3:8b"]):
            self.run_setup(["--list-models"])
        self.launcher.find_executable.assert_not_called()
        self.assertFalse((self.root / "opencode.json").exists())
        self.assertFalse((self.root / ".env.opencode").exists())
        self.assertIn("qwen3:8b", self.output.getvalue())

    def test_numeric_picker_and_invalid_input_require_an_explicit_choice(self):
        with patch("builtins.input", side_effect=["typo", "2"]), contextlib.redirect_stdout(self.output):
            self.assertEqual(self.setup.select_model(["first", "second"], "current"), "second")
        with patch("builtins.input", return_value=""):
            self.assertEqual(self.setup.select_model(["first"], "current"), "current")

    def test_fresh_setup_prompts_for_url_and_key_and_selects_from_discovery(self):
        with patch.object(self.setup, "ROOT", self.root), patch.dict(os.environ, {}, clear=True), \
             patch.object(self.setup.sys.stdin, "isatty", return_value=True), \
             patch("builtins.input", side_effect=["https://provided.example/swarm/api/v1", "2"]), \
             patch.object(self.setup.getpass, "getpass", return_value="provided-key"), \
             patch.object(self.setup, "check_connection", return_value=["qwen3:30b-a3b", "qwen3:8b"]) as discover, \
             patch.object(self.setup, "load_launcher", return_value=self.launcher), \
             patch.object(self.setup.subprocess, "run", return_value=Mock(stdout="1.18.32\n")), \
             contextlib.redirect_stdout(self.output):
            self.setup.main([])
        discover.assert_called_once_with("https://provided.example/swarm/api/v1", "provided-key")
        config = json.loads((self.root / "opencode.json").read_text())
        self.assertEqual(config["model"], "swarm-llm/qwen3:8b")
        self.assertEqual(config["small_model"], config["model"])
        self.assertNotIn("provided-key", self.output.getvalue() + json.dumps(config))

    def test_missing_url_fails_before_sending_the_consumer_key(self):
        with patch.object(self.setup, "ROOT", self.root), \
             patch.dict(os.environ, {"SWARM_CONSUMER_KEY": "secret"}, clear=True), \
             patch.object(self.setup.sys.stdin, "isatty", return_value=False), \
             patch.object(self.setup, "check_connection") as discover, \
             contextlib.redirect_stderr(self.output):
            with self.assertRaises(SystemExit):
                self.setup.main([])
        discover.assert_not_called()
        self.assertFalse((self.root / "opencode.json").exists())

    def test_existing_url_and_selected_model_are_reused_without_an_implicit_model_change(self):
        path = self.root / "opencode.json"
        path.write_text(json.dumps({"model": "swarm-llm/gemma4:12b", "provider": {"swarm-llm": {
            "options": {"baseURL": "https://saved.example/v1"}}}}))
        with patch.object(self.setup, "ROOT", self.root), \
             patch.dict(os.environ, {"SWARM_CONSUMER_KEY": "secret"}, clear=True), \
             patch.object(self.setup.sys.stdin, "isatty", return_value=False), \
             patch.object(self.setup, "check_connection", return_value=["qwen3:8b"]) as discover, \
             patch.object(self.setup, "load_launcher", return_value=self.launcher), \
             patch.object(self.setup.subprocess, "run", return_value=Mock(stdout="1.18.32\n")), \
             contextlib.redirect_stdout(self.output):
            self.setup.main([])
        discover.assert_called_once_with("https://saved.example/v1", "secret")
        config = json.loads(path.read_text())
        self.assertEqual(config["model"], "swarm-llm/gemma4:12b")
        self.assertIn("gemma4:12b", config["provider"]["swarm-llm"]["whitelist"])

    def test_offline_refresh_keeps_saved_picker_and_context_overrides(self):
        path = self.root / "opencode.json"
        profile = self.config.build_config("qwen3:30b-a3b", context=16384, advertised=["qwen3:30b-a3b"])
        profile["provider"]["swarm-llm"]["models"]["hidden-old-model"] = {"name": "keep hidden"}
        path.write_text(json.dumps(profile))
        self.run_setup(["--offline"])
        provider = json.loads(path.read_text())["provider"]["swarm-llm"]
        self.assertEqual(provider["whitelist"], ["qwen3:30b-a3b"])
        self.assertEqual(provider["models"]["qwen3:30b-a3b"]["limit"]["context"], 16384)
        self.run_setup(["--offline", "--context", "8192"])
        provider = json.loads(path.read_text())["provider"]["swarm-llm"]
        self.assertEqual(provider["models"]["qwen3:30b-a3b"]["options"]["options"]["num_ctx"], 8192)

    def test_separate_title_model_preserves_coding_and_summary_quality_settings(self):
        with patch.object(self.setup, "check_connection", return_value=["qwen3:30b-a3b", "qwen3:14b"]):
            self.run_setup(["--title-model", "qwen3:14b"])
        config = json.loads((self.root / "opencode.json").read_text())
        self.assertEqual(config["model"], "swarm-llm/qwen3:30b-a3b")
        self.assertEqual(config["small_model"], config["model"])
        self.assertEqual(config["agent"]["title"]["model"], "swarm-llm/qwen3:14b")
        self.assertEqual(config["provider"]["swarm-llm"]["models"]["qwen3:30b-a3b"]["limit"]["context"], 32768)
        with patch.object(self.setup, "check_connection", return_value=[]):
            with self.assertRaises(SystemExit):
                self.run_setup(["--title-model", "unknown"])
        self.assertEqual(json.loads((self.root / "opencode.json").read_text()), config)


class DiscoveryTests(unittest.TestCase):
    def test_authenticated_discovery_and_rejection_do_not_follow_redirects_or_log_secrets(self):
        setup = load("opencode_setup_discovery_test", ROOT / "scripts/setup-opencode.py")
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("X-API-Key")))
                code = 302 if self.path.startswith("/redirect") else 401 if self.path.startswith("/bad") else 200
                self.send_response(code)
                self.send_header("Location", "/leaked")
                self.end_headers()
                self.wfile.write(json.dumps({"data": [{"id": "qwen3:30b-a3b"}]}).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%s" % server.server_port
            self.assertEqual(setup.check_connection(base + "/v1", "secret"), ["qwen3:30b-a3b"])
            for prefix in ("/bad", "/redirect"):
                with self.assertRaises(ValueError) as error:
                    setup.check_connection(base + prefix, "secret")
                self.assertNotIn("secret", str(error.exception))
            self.assertEqual(requests, [("/v1/models", "secret"), ("/bad/models", "secret"), ("/redirect/models", "secret")])
            for url in ("http://remote.example/v1", "https://user:secret@host/v1", "https://host/v1?secret"):
                with self.assertRaises(ValueError):
                    setup.check_connection(url, "secret")
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    unittest.main()
