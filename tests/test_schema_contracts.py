"""Validate the API boundaries without reserving GPUs or exposing bad payloads."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from coordinator.chat_admission import InvalidChatRequest, prepare_chat_request
from coordinator.persistence import CoordinatorRepository
from coordinator.relay_policy import RelayPolicy
from coordinator.rpc_performance import validate_runtime
from coordinator.validation import contract
from scheduling_support import SchedulingFixture


class SchemaContractTests(unittest.TestCase):
    def test_chat_normalization_retains_client_extensions_and_incomplete_arguments(
        self,
    ):
        body = {
            "model": "custom/model",
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"arguments": '{"incomplete":'}}],
                },
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            ],
            "tools": [
                {
                    "function": {
                        "parameters": {"type": "object", "additionalProperties": True}
                    }
                }
            ],
            "max_tokens": 64,
            "max_completion_tokens": 32,
            "options": {"num_predict": 16},
            "client_extension": {"kept": True},
        }
        result = prepare_chat_request(deepcopy(body))
        self.assertEqual(result["options"]["num_predict"], 16)
        self.assertEqual(
            result["messages"][1]["tool_calls"][0]["function"]["arguments"],
            '{"incomplete":',
        )
        self.assertEqual(result["client_extension"], body["client_extension"])
        self.assertEqual(result["tools"], body["tools"])

    def test_validation_is_atomic_and_does_not_echo_submitted_values(self):
        body = {
            "model": "model",
            "messages": [{"role": "user", "content": "private-prompt"}],
            "options": {"num_predict": "private-invalid-value"},
        }
        original = deepcopy(body)
        with self.assertRaises(InvalidChatRequest) as error:
            prepare_chat_request(body)
        self.assertEqual(body, original)
        self.assertIn("num_predict", str(error.exception))
        self.assertNotIn("private-invalid-value", str(error.exception))
        self.assertNotIn("private-prompt", str(error.exception))

    def test_runtime_preserves_strict_integers_and_cross_field_limits(self):
        for value in (
            {"batch_size": 128.0},
            {"batch_size": True},
            {"ubatch_size": 256},
            {"cache_ram": None},
            {"custom_native_flag": "unsafe"},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_runtime(value)
        self.assertEqual(
            validate_runtime({"batch_size": 512, "ubatch_size": 256})["ubatch_size"],
            256,
        )

    def test_malformed_relay_entries_fail_with_value_error_before_field_access(self):
        for entry in (
            None,
            7,
            [],
            "relay",
            {"id": "relay", "url": 5, "control_key": "private"},
            {
                "id": "relay",
                "url": "https://relay.example",
                "control_key": "private",
                "agent_ids": [None],
            },
        ):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                RelayPolicy(json.dumps([entry]))

    def test_stream_contract_accepts_usage_only_frames_and_rejects_malformed_deltas(
        self,
    ):
        validator = contract("stream-chunks")
        self.assertTrue(
            validator.is_valid([{"choices": [], "usage": {"total_tokens": 3}}])
        )
        for choices in (
            None,
            [None],
            [{"delta": []}],
            [{"delta": {"content": 2}}],
            [{"delta": {"tool_calls": [{"index": True}]}}],
            [{"delta": {"tool_calls": [{"index": 0.0}]}}],
        ):
            with self.subTest(choices=choices):
                self.assertFalse(validator.is_valid([{"choices": choices}]))

    def test_repositories_have_isolated_paths_and_restore_machine_state(self):
        from coordinator.api_models import RegisterPayload
        from coordinator.models import MachineState

        with tempfile.TemporaryDirectory() as directory:
            repositories = [
                CoordinatorRepository(lambda name=name: str(Path(directory) / name))
                for name in ("first.sqlite", "second.sqlite")
            ]
            for repository in repositories:
                repository.init_db()
            machine = MachineState(
                "127.0.0.1", RegisterPayload(agent_id="gpu", port=11434, model="custom")
            )
            repositories[0].upsert_machine(machine)
            self.assertEqual(repositories[1].load_machines(), {})
            restored = repositories[0].load_machines()["gpu"]
            self.assertEqual(restored.model, "custom")
            self.assertFalse(restored.alive)


class SchemaAdmissionTests(SchedulingFixture):
    async def test_invalid_wire_shapes_do_not_create_jobs_clusters_or_reservations(
        self,
    ):
        self.machine("one")
        self.machine("two")
        base = {
            "model": "qwen3:30b-a3b",
            "messages": [{"role": "user", "content": "hello"}],
        }
        for override in (
            {"messages": [None]},
            {"options": []},
            {"model": "  "},
            {"messages": [{"role": "unknown"}]},
            {"max_tokens": True},
            {"max_tokens": 2.0},
            {"stream": "true"},
            {"messages": [{"role": "user", "tool_calls": [{"function": None}]}]},
        ):
            response = await self.client.post(
                "/v1/chat/completions",
                headers={"X-API-Key": "consumer"},
                json={**base, **override},
            )
            self.assertEqual(response.status_code, 422, response.text)
        self.assertFalse(self.c.jobs)
        self.assertFalse(self.c.rpc_clusters)
        self.assertFalse(
            any(machine.busy or machine.in_rpc for machine in self.c.machines.values())
        )

    async def test_extracted_auth_preserves_role_separation_and_bearer_support(self):
        for key in ("agent", "consumer", "wrong"):
            response = await self.client.get(
                "/machines", headers={"Authorization": "Bearer " + key}
            )
            self.assertEqual(response.status_code, 401)
        self.assertEqual(
            (
                await self.client.get(
                    "/machines", headers={"Authorization": "Bearer admin"}
                )
            ).status_code,
            200,
        )
        self.assertEqual(
            (
                await self.client.get(
                    "/v1/models", headers={"Authorization": "Bearer consumer"}
                )
            ).status_code,
            200,
        )

    async def test_application_catalog_customization_does_not_modify_shared_policy(
        self,
    ):
        from coordinator.catalog import _RPC_MODELS

        self.c._RPC_MODELS["custom:large"] = 42
        self.assertNotIn("custom:large", _RPC_MODELS)
