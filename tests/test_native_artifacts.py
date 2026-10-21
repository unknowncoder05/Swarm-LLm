"""Native model artifacts are pinned, bounded and atomically verified on agents."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_agent_reports import ROOT, load


class NativeArtifactTests(unittest.TestCase):
    def setUp(self):
        self.agent = load("native_artifact_agent", ROOT / "agents/agent.py")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = b"GGUF-test-text-model"
        self.model = "devstral-small-2:24b"
        self.artifact = dict(self.agent._NATIVE_MODEL_ARTIFACTS[self.model],
            size=len(self.data), sha256=hashlib.sha256(self.data).hexdigest())
        for context in (
            patch.dict(self.agent._NATIVE_MODEL_ARTIFACTS, {self.model: self.artifact}),
            patch.object(self.agent, "_context_paths", return_value={"root": self.root}),
            patch.object(self.agent, "write_status"),
        ):
            context.start()
            self.addCleanup(context.stop)

    def response(self, data):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [data[:4], b"", data[4:]]
        return response

    def test_verified_download_is_pinned_atomic_and_reused_without_network(self):
        with patch.object(self.agent.requests, "get", return_value=self.response(self.data)) as get:
            target = Path(self.agent.resolve_native_gguf(self.model))
            self.assertEqual(target.read_bytes(), self.data)
            self.assertIn(self.artifact["revision"], get.call_args.args[0])
            self.assertTrue(get.call_args.kwargs["stream"])
            self.assertFalse(target.with_suffix(".partial").exists())
            self.assertEqual(self.agent.resolve_native_gguf(self.model), str(target))
            get.assert_called_once()

    def test_bad_digest_or_truncated_or_oversized_data_never_becomes_a_cached_model(self):
        for data in (b"X" * len(self.data), self.data[:-1], self.data + b"extra"):
            with self.subTest(data=data), patch.object(self.agent.requests, "get", return_value=self.response(data)):
                with self.assertRaises(RuntimeError):
                    self.agent.resolve_native_gguf(self.model)
                self.assertEqual(list((self.root / "models").iterdir()), [])

    def test_low_disk_space_fails_before_downloading(self):
        with patch.object(self.agent.shutil, "disk_usage", return_value=Mock(free=1)), \
                patch.object(self.agent.requests, "get") as get:
            with self.assertRaisesRegex(RuntimeError, "disk space"):
                self.agent.resolve_native_gguf(self.model)
            get.assert_not_called()

    def test_other_native_models_still_reuse_ollama_cache(self):
        with patch.object(self.agent, "resolve_gguf", return_value="cached.gguf") as resolve:
            self.assertEqual(self.agent.resolve_native_gguf("qwen3:30b-a3b"), "cached.gguf")
            resolve.assert_called_once_with("qwen3:30b-a3b")
