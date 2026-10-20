"""Keep the coding client on the requested model and a usable context budget."""
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch
from test_agent_reports import ROOT, load


class OpenCodeConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load("opencode_config", ROOT / "scripts/gen_opencode_config.py")

    def test_model_selection_prefers_advertised_distributed_model_and_honors_override(self):
        self.assertEqual(self.config.choose_model(["qwen3:14b", "qwen3:30b-a3b"]), "qwen3:30b-a3b")
        self.assertEqual(self.config.choose_model(["qwen3:14b"]), "qwen3:14b")
        self.assertEqual(self.config.choose_model([], "qwen3:30b-a3b"), "qwen3:30b-a3b")
        self.assertEqual(self.config.choose_model([]), "qwen2.5-coder:1.5b")

    def test_metadata_and_wire_context_agree_without_disabling_reasoning(self):
        config = self.config.build_config("qwen3:30b-a3b")
        provider = config["provider"]["swarm-llm"]
        model = provider["models"]["qwen3:30b-a3b"]
        self.assertEqual(config["model"], "swarm-llm/qwen3:30b-a3b")
        self.assertEqual(config["small_model"], config["model"])
        self.assertEqual(model["limit"]["context"], 32768)
        self.assertEqual(model["options"]["options"]["num_ctx"], 32768)
        self.assertNotIn("think", model["options"]["options"])
        self.assertEqual(provider["options"]["apiKey"], "{env:SWARM_CONSUMER_KEY}")
        self.assertGreater(provider["options"]["headerTimeout"], 3600000)

    def test_context_override_and_native_limit(self):
        config = self.config.build_config("qwen3:30b-a3b", context=16384)
        model = config["provider"]["swarm-llm"]["models"]["qwen3:30b-a3b"]
        self.assertEqual(model["limit"]["context"], 16384)
        self.assertEqual(model["options"]["options"]["num_ctx"], 16384)
        for context in (True, 511, 32769, 32768.0):
            with self.subTest(context=context), self.assertRaises(ValueError):
                self.config.build_config("qwen3:30b-a3b", context=context)

    def test_launcher_corrects_stale_pwd_and_forwards_the_requested_directory(self):
        launcher = load("opencode_launcher", ROOT / "scripts/run-opencode.py")
        with patch.dict(launcher.os.environ, {"SWARM_CONSUMER_KEY": "test-consumer", "PWD": "/old/project"}), \
             patch.object(launcher.shutil, "which", return_value="/bin/opencode"), \
             patch.object(launcher.os, "getcwd", return_value="/tmp/current-project"), \
             patch.object(launcher.sys, "argv", ["run-opencode.py", "run", "--dir", "/tmp/fixture", "hi"]), \
             patch.object(launcher.os, "execve") as execute:
            launcher.main()
        binary, argv, env = execute.call_args.args
        self.assertEqual(argv, [binary, "run", "--dir", "/tmp/fixture", "hi"])
        self.assertEqual(env["PWD"], "/tmp/current-project")
        self.assertEqual(env["SWARM_CONSUMER_KEY"], "test-consumer")

    def test_launcher_resolves_windows_npm_shim_to_native_executable(self):
        launcher = load("opencode_windows_launcher", ROOT / "scripts/run-opencode.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native = root / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
            native.parent.mkdir(parents=True)
            native.write_bytes(b"test executable")
            with patch.object(launcher, "ROOT", root), \
                 patch.dict(launcher.os.environ, {"SWARM_CONSUMER_KEY": "test-consumer"}), \
                 patch.object(launcher.shutil, "which", return_value=str(root / "opencode.cmd")), \
                 patch.object(launcher.sys, "argv", ["run-opencode.py", "--version"]), \
                 patch.object(launcher.os, "execve") as execute:
                launcher.main()
            self.assertEqual(execute.call_args.args[0], str(native))


if __name__ == "__main__":
    unittest.main()
