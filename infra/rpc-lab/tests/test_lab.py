import importlib.util
import json
import signal
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = load("lab_smoke", ROOT / "scripts/smoke.py")
wrapper = load("lab_wrapper", ROOT / "scripts/llama-wrapper.py")
launch = load("lab_launch", ROOT / "scripts/launch.py")
trial = load("lab_trial", ROOT / "trial.py")


class CapacityTests(unittest.TestCase):
    def test_rejects_single_gpu_and_insufficient_total(self):
        with self.assertRaises(RuntimeError):
            smoke.verify_capacity([{"alive": True, "gpu_vram_gb": 24}], 1)
        with self.assertRaises(RuntimeError):
            smoke.verify_capacity([{"alive": True, "gpu_vram_gb": 8}] * 2, 2)

    def test_requires_all_nodes_alive_and_real_gpu_memory(self):
        nodes = [{"alive": True, "gpu_vram_gb": 15.0}] * 2
        self.assertEqual(smoke.verify_capacity(nodes, 2), nodes)
        with self.assertRaises(RuntimeError):
            smoke.verify_capacity(nodes + [{"alive": False, "gpu_vram_gb": 15}], 3)
        with self.assertRaises(RuntimeError):
            smoke.verify_capacity(nodes + [{"alive": True, "gpu_vram_gb": 0}], 3)

    def test_fractional_gpus_require_model_larger_than_one_device(self):
        nodes = [{"alive": True, "gpu_vram_gb": 2.79}] * 2
        self.assertEqual(smoke.verify_capacity(nodes, 2, 3.6), nodes)
        with self.assertRaises(RuntimeError):
            smoke.verify_capacity(nodes, 2, 2.0)

    def test_cluster_requires_remote_workers_and_reservations(self):
        state = {
            "rpc_clusters": [{"model": "qwen3:32b", "status": "active", "primary_id": "a", "worker_ids": ["b"]}],
            "machines": [{"key": key, "alive": True, "in_rpc": True} for key in ("a", "b")],
        }
        smoke.verify_cluster(state, "qwen3:32b")
        with self.assertRaises(RuntimeError):
            smoke.verify_cluster(state, "qwen3:32b", 3)
        state["machines"][1]["in_rpc"] = False
        with self.assertRaises(RuntimeError):
            smoke.verify_cluster(state, "qwen3:32b")
        state["rpc_clusters"][0]["worker_ids"] = []
        with self.assertRaises(RuntimeError):
            smoke.verify_cluster(state, "qwen3:32b")


class AdapterTests(unittest.TestCase):
    def test_primary_preserves_rpc_peers_and_uses_supported_loading_flags(self):
        cmd = wrapper.command("llama-server", ["--no-mmap", "-m", "model.gguf", "-ngl", "999", "--rpc", "10.44.0.2:50052"])
        self.assertNotIn("--no-mmap", cmd)
        self.assertEqual(cmd[cmd.index("--load-mode") + 1], "mmap")
        self.assertEqual(cmd[cmd.index("--rpc") + 1], "10.44.0.2:50052")
        self.assertEqual(cmd[cmd.index("--parallel") + 1], "1")
        self.assertEqual(cmd[cmd.index("-ngl") + 1], "auto")
        self.assertEqual(cmd[cmd.index("--fit-target") + 1], "512")

    def test_worker_executes_upstream_binary_and_keeps_assigned_port(self):
        cmd = wrapper.command("llama-rpc-server", ["--host", "0.0.0.0", "--port", "50053"])
        self.assertTrue(cmd[0].endswith("ggml-rpc-server"))
        self.assertEqual(cmd[-1], "50053")

    def test_context_override_does_not_mutate_coordinator_assignment(self):
        calls = []
        agent = SimpleNamespace(run_rpc_primary=lambda *args: calls.append(args),
                                model_vram=lambda model: 2.6,
                                _wait_llama_ready=lambda port, timeout: (port, timeout))
        launch.configure_agent(agent, {"context_size": 8192, "model": "llama3.2:3b-instruct-q8_0"})
        assignment = {"ctx_size": 16384, "workers": ["10.44.0.2:50052"]}
        agent.run_rpc_primary(assignment, "http://coordinator", "key")
        self.assertEqual(assignment["ctx_size"], 16384)
        self.assertEqual(calls[0][0]["ctx_size"], 8192)
        self.assertEqual(calls[0][0]["workers"], assignment["workers"])
        self.assertEqual(agent._wait_llama_ready(8080, 300), (8080, 900))
        self.assertEqual(agent.model_vram("llama3.2:3b-instruct-q8_0"), float("inf"))
        self.assertEqual(agent.model_vram("another:model"), 2.6)


class CleanupTests(unittest.TestCase):
    def test_termination_signal_still_collects_evidence_and_destroys(self):
        calls = []
        lab = {"region": "us-east-1", "coordinator_id": "i-cpu", "gpu_ids": ["i-a", "i-b"]}

        def terraform(*args, **kwargs):
            calls.append(args)
            return lab if args[0] == "output" else None

        def terminate(_lab):
            trial.interrupt_for_cleanup(signal.SIGTERM, None)

        with patch.object(trial, "terraform", side_effect=terraform), \
             patch.object(trial, "wait_ready", side_effect=terminate), \
             patch.object(trial, "evidence", return_value={"logs": "captured"}), \
             patch.object(trial.Path, "write_text") as write, \
             patch("sys.argv", ["trial.py", "--test-existing"]):
            self.assertEqual(trial.main(), 1)
        self.assertEqual(calls[-1], ("destroy", "-auto-approve"))
        report = json.loads(write.call_args[0][0])
        self.assertEqual(report["error"], "Received SIGTERM")
        self.assertEqual(report["evidence"], {"logs": "captured"})

    def test_gpu_evidence_requires_llama_allocation_on_every_node(self):
        lab = {"gpu_ids": ["i-a", "i-b"]}
        observations = {
            key: {"Status": "Success", "StandardOutputContent":
                  "--- GPU processes ---\n/opt/bin/{}, 10240\n--- device buffers ---\n".format(name)}
            for key, name in (("i-a", "llama-server"), ("i-b", "ggml-rpc-server"))
        }
        self.assertEqual(len(trial.verify_gpu_evidence(lab, observations)), 2)
        observations["i-b"]["StandardOutputContent"] = "--- GPU processes ---\npython3, 2000\n--- device buffers ---"
        with self.assertRaises(RuntimeError):
            trial.verify_gpu_evidence(lab, observations)

    def test_disk_full_during_evidence_does_not_skip_destroy(self):
        calls = []
        lab = {"region": "us-east-1", "coordinator_id": "i-cpu", "gpu_ids": ["i-a", "i-b"]}

        def terraform(*args, **kwargs):
            calls.append(args)
            return lab if args[0] == "output" else None

        with patch.object(trial, "terraform", side_effect=terraform), \
             patch.object(trial, "wait_ready", side_effect=RuntimeError("failed boot")), \
             patch.object(trial, "evidence", return_value={}), \
             patch.object(trial.Path, "write_text", side_effect=OSError("No space left")), \
             patch("sys.argv", ["trial.py", "--test-existing"]):
            self.assertEqual(trial.main(), 1)
        self.assertEqual(calls[-1], ("destroy", "-auto-approve"))

    def test_apply_failure_still_runs_destroy(self):
        calls = []

        def terraform(*args, **kwargs):
            calls.append(args)
            if args == ("apply",):
                raise RuntimeError("Spot capacity unavailable")

        with patch.object(trial, "terraform", side_effect=terraform), \
             patch.object(trial.Path, "write_text") as write, \
             patch("sys.argv", ["trial.py"]):
            self.assertEqual(trial.main(), 1)
        self.assertIn(("destroy", "-auto-approve"), calls)
        self.assertIn("Spot capacity unavailable", json.loads(write.call_args[0][0])["error"])

    def test_inference_failure_collects_evidence_before_destroy(self):
        calls = []
        lab = {"region": "us-east-1", "coordinator_id": "i-cpu", "gpu_ids": ["i-a", "i-b"]}

        def terraform(*args, **kwargs):
            calls.append(args)
            return lab if args[0] == "output" else None

        def evidence(config):
            self.assertNotIn(("destroy", "-auto-approve"), calls)
            return {"logs": "captured"}

        with patch.object(trial, "terraform", side_effect=terraform), \
             patch.object(trial, "wait_ready"), patch.object(trial, "send", return_value="command-id"), \
             patch.object(trial, "wait_command", return_value={"Status": "Failed", "StandardErrorContent": "RPC unreachable"}), \
             patch.object(trial, "evidence", side_effect=evidence), \
             patch.object(trial.Path, "write_text") as write, \
             patch("sys.argv", ["trial.py", "--test-existing"]):
            self.assertEqual(trial.main(), 1)
        self.assertEqual(calls[-1], ("destroy", "-auto-approve"))
        self.assertEqual(json.loads(write.call_args[0][0])["evidence"], {"logs": "captured"})


if __name__ == "__main__":
    unittest.main()
