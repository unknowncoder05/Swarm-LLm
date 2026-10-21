"""Native model artifacts are pinned, bounded and atomically verified on agents."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
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

    def test_native_http_errors_preserve_status_and_context_detail_in_both_response_modes(self):
        a = self.agent
        for emit in (None, Mock()):
            response = Mock(status_code=400, text='{"error":"request (11845 tokens) exceeds context (4096 tokens)"}')
            response.raise_for_status.side_effect = a.requests.HTTPError("bad request")
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            with patch.object(a.requests, "post", return_value=response):
                with self.assertRaises(a.NativeRequestError) as error:
                    a.rpc_infer({"model": self.model, "messages": [], "options": {}}, 18080, emit)
                self.assertEqual(error.exception.status_code, 400)
                self.assertIn("11845", str(error.exception))

    def test_devstral_primary_uses_verified_author_template_for_coding_clients(self):
        a = self.agent
        with ExitStack() as stack:
            for name, value in {"resolve_native_gguf": "model.gguf", "ensure_llamacpp": {"llama_server": "server"},
                    "_stop_ollama_for_rpc": None, "_available_loopback_port": 18080,
                    "_wait_llama_ready": True, "_read_rpc_offload_info": (41, 41),
                    "_confirm_rpc_ready": False}.items():
                stack.enter_context(patch.object(a, name, return_value=value))
            cached = stack.enter_context(patch.object(a, "_cached_native_artifact", return_value="verified.jinja"))
            launch = stack.enter_context(patch.object(a, "_launch_rpc_process"))
            a.run_rpc_primary({"cluster_id": "trial", "model": self.model, "ctx_size": 16384}, "http://test", "key")
            command = launch.call_args.args[0]
            self.assertEqual(command[command.index("--chat-template-file") + 1], "verified.jinja")
            cached.assert_called_once_with(a._NATIVE_CHAT_TEMPLATES[self.model], self.model)
