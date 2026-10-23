"""Coding clients need tool indexes, reasoning, and usage in SSE responses."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from test_agent_reports import ROOT, load


class OpenAIStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"ADMIN_KEY": "admin", "AGENT_KEY": "agent", "CONSUMER_KEY": "consumer",
                                     "RPC_RELAY_URL": "", "RPC_RELAY_ADMIN_KEY": ""}):
            self.c = load("openai_stream_coordinator", ROOT / "coordinator/coordinator.py")
        self.c.DB_PATH = str(Path(self.temp.name) / "db.sqlite")
        self.c._init_db()
        cluster = self.c.RpcCluster("trial", "qwen3:30b-a3b", "primary", ["worker"])
        cluster.status, cluster.primary_ready = "active", True
        self.c.rpc_clusters["trial"] = cluster
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.c.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def stream(self, result, include_usage=False):
        task = asyncio.create_task(self.client.post("/v1/chat/completions", headers={"X-API-Key": "consumer"},
            json={"model": "qwen3:30b-a3b", "stream": True, "stream_options": {"include_usage": include_usage},
                  "options": {"num_ctx": 4096},  # Match the pre-existing fixture runtime.
                  "messages": [{"role": "user", "content": "Read these two files"}]}))
        for _ in range(100):
            if self.c.jobs or task.done():
                break
            await asyncio.sleep(.001)
        self.assertTrue(self.c.jobs, "Request did not create an inference job")
        job = next(iter(self.c.jobs.values()))
        job.result = copy.deepcopy(result)
        job.done_event.set()
        response = await asyncio.wait_for(task, timeout=2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
        lines = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(lines[-1], "[DONE]")
        return [json.loads(line) for line in lines[:-1]]

    async def test_multiple_tool_calls_have_distinct_stream_indexes_and_keep_arguments(self):
        calls = [{"id": "call-a", "type": "function", "function": {"name": "read", "arguments": '{"path":"a.py"}'}},
                 {"id": "call-b", "type": "function", "function": {"name": "read", "arguments": '{"path":"b.py"}'}}]
        chunks = await self.stream({"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": calls, "reasoning_content": "I will inspect both files."}, "finish_reason": "tool_calls"}]})
        deltas = [choice["delta"] for chunk in chunks for choice in chunk["choices"]]
        actual = next(delta["tool_calls"] for delta in deltas if "tool_calls" in delta)
        self.assertEqual([call["index"] for call in actual], [0, 1])
        self.assertEqual([{k: v for k, v in call.items() if k != "index"} for call in actual], calls)
        self.assertEqual(next(d["reasoning_content"] for d in deltas if "reasoning_content" in d),
                         "I will inspect both files.")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")

    async def test_usage_is_sent_when_requested_without_extra_generation(self):
        usage = {"prompt_tokens": 1024, "completion_tokens": 42, "total_tokens": 1066}
        result = {"choices": [{"message": {"role": "assistant", "content": "Done"}, "finish_reason": "stop"}],
                  "usage": usage}
        chunks = await self.stream(result, include_usage=True)
        self.assertEqual(chunks[-1]["choices"], [])
        self.assertEqual(chunks[-1]["usage"], usage)
        self.assertEqual(chunks[-2]["choices"][0]["finish_reason"], "stop")
        chunks = await self.stream(result, include_usage=False)
        self.assertFalse(any("usage" in chunk for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
