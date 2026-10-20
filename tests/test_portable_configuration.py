"""Publication defaults must work without identifying a private deployment."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_agent_reports import ROOT, load


class PortableConfigurationTests(unittest.TestCase):
    def test_profile_reads_url_from_environment_and_private_files_with_correct_precedence(self):
        config = load("portable_client_config", ROOT / "scripts/gen_opencode_config.py")
        self.assertEqual(config.build_config("qwen3:30b-a3b")["provider"]["swarm-llm"]["options"]["baseURL"],
                         "{env:SWARM_BASE_URL}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text('SWARM_BASE_URL="https://server.example/v1"\n')
            self.assertEqual(config.load_base_url(root, {}), "https://server.example/v1")
            (root / ".env.opencode").write_text('SWARM_CONSUMER_KEY="preserve-me"\nOTHER=value\n')
            config.save_client_settings(root, "https://client.example/v1")
            self.assertEqual(config.load_base_url(root, {}), "https://client.example/v1")
            self.assertEqual(config.load_consumer_key(root, {}), "preserve-me")
            self.assertIn("OTHER=value", (root / ".env.opencode").read_text())
            self.assertEqual(config.load_base_url(root, {"SWARM_BASE_URL": "https://env.example/v1"}),
                             "https://env.example/v1")

    def test_initializer_preserves_existing_credentials_and_private_configuration(self):
        module = load("portable_init_env", ROOT / "scripts/init-env.py")
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
            path = Path(directory) / ".env"
            module.initialize(path)
            first = path.read_bytes()
            values = dict(line.split("=", 1) for line in first.decode().splitlines()
                          if "=" in line and not line.startswith("#"))
            for name in ("ADMIN_KEY", "AGENT_KEY", "CONSUMER_KEY"):
                self.assertEqual(len(values[name]), 64)
                self.assertNotIn(values[name], output.getvalue())
            self.assertEqual(values["RPC_DEFAULT_TRANSPORT"], "auto")
            path.write_bytes(first + b'CUSTOM_SETTING=keep\n')
            module.initialize(path)
            self.assertEqual(path.read_bytes(), first + b'CUSTOM_SETTING=keep\n')
            if os.name != "nt": self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_fresh_checkout_includes_agent_and_its_pinned_transport_helper(self):
        import hashlib
        agent = ROOT / "agents/agent.py"
        helper = ROOT / "agents/quic_transport.py"
        self.assertTrue(agent.is_file())
        self.assertFalse((ROOT / "agents/.git").exists())
        self.assertIn(hashlib.sha256(helper.read_bytes()).hexdigest(), agent.read_text())

    def test_launcher_supplies_private_deployment_url_to_environment_profile(self):
        launcher = load("portable_launcher", ROOT / "scripts/run-opencode.py")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.opencode").write_text('SWARM_BASE_URL="https://private.example/v1"\nSWARM_CONSUMER_KEY="key"\n')
            with patch.object(launcher, "ROOT", root), patch.dict(os.environ, {}, clear=True), \
                 patch.object(launcher, "find_executable", return_value="/fake/opencode"), \
                 patch.object(launcher.os, "execve") as execute:
                launcher.main()
            self.assertEqual(execute.call_args.args[2]["SWARM_BASE_URL"], "https://private.example/v1")
