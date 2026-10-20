"""Real-time deltas must remain bounded, fenced, and consistent with final usage."""
import asyncio
import json
import unittest
from unittest.mock import Mock, patch
from scheduling_support import SchedulingFixture
from test_agent_reports import ROOT, load


class LiveStreamTests(SchedulingFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.machine("primary").runtime = {"rpc_quic": True, "rpc_stream": True}
        self.machine("worker").runtime = {"rpc_quic": True}
        self.cluster = self.c._form_rpc_cluster("qwen3:30b-a3b", 19, 32768, "quic")
        self.cluster.status = "active"; self.cluster.primary_ready = True; self.cluster.automatic = True
        self.job = self.enqueue(self.cluster.model, 32768)
        self.job.rpc_cluster_id = self.cluster.cluster_id
        self.job.live_stream = self.c.StreamBuffer()
        self.claimed = (await self.claim(self.cluster.primary_id, self.cluster.model)).json()
        self.url = "/agent/jobs/" + self.job.id + "/events"

    async def event(self, seq=0, token=None, chunks=None):
        return await self.client.post(self.url, headers=self.headers, json={"agent_id": self.cluster.primary_id,
            "claim_token": token or self.claimed["claim_token"], "sequence": seq,
            "chunks": chunks or [{"choices": [{"index": 0, "delta": {"content": "hello"}}]}]})

    async def test_content_arrives_before_completion_and_usage_is_committed_once(self):
        request = Mock(); request.is_disconnected = Mock(side_effect=lambda: asyncio.sleep(0, result=False))
        stream = self.c._stream_chat_response(self.job, request, "test", self.job.created_at, True)
        first = asyncio.create_task(anext(stream))
        response = await self.event(chunks=[{"choices": [{"index": 0, "delta": {"reasoning_content": "thinking"}}]}])
        self.assertEqual(response.status_code, 200)
        self.assertIn("thinking", await asyncio.wait_for(first, 2))
        self.assertFalse(self.job.done_event.is_set())
        self.assertEqual(len(self.c.active_chat_streams), 1)
        self.assertEqual((await self.event(1)).status_code, 200)
        result = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23}}
        await self.client.post("/agent/jobs/" + self.job.id + "/result", headers=self.headers,
            json={"claim_token": self.claimed["claim_token"], "result": result})
        stop = await self.client.post("/rpc/clusters/" + self.cluster.cluster_id + "/stop", headers={"X-API-Key": "admin"})
        self.assertEqual(stop.status_code, 409, "The consumer still needs its remaining SSE chunks")
        remaining = [item async for item in stream]
        self.assertEqual(remaining[-1], "data: [DONE]\n\n")
        self.assertEqual(sum('"usage"' in item for item in remaining), 1)
        self.assertEqual(sum('"finish_reason": "stop"' in item for item in remaining), 1)
        self.assertFalse(self.c.jobs)
        self.assertFalse(self.c.active_chat_streams)

    async def test_retry_is_idempotent_and_changed_duplicate_or_stale_claim_is_rejected(self):
        self.assertEqual((await self.event()).status_code, 200)
        self.assertEqual((await self.event()).status_code, 200)
        self.assertEqual(self.job.live_stream.queue.qsize(), 1)
        self.assertEqual((await self.event(token="stale")).status_code, 409)
        self.assertEqual((await self.event(2)).status_code, 409)
        self.assertEqual((await self.event(chunks=[{"choices": []}])).status_code, 409)
        self.c._cancel_job(self.job.id, "test")
        self.assertEqual((await self.event(1)).status_code, 410)

    async def test_partial_stream_is_not_replayed_by_automatic_route_recovery(self):
        await self.event()
        self.c._dissolve_cluster(self.cluster, "inference_failed")
        self.assertTrue(self.job.cancelled)
        self.assertFalse(self.job.rpc_recovering)

    async def test_large_batches_and_invalid_tool_indexes_are_rejected(self):
        response = await self.client.post(self.url, headers=self.headers, content=b"x" * 65537)
        self.assertEqual(response.status_code, 413)
        response = await self.event(chunks=[{"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 999}]}}]}])
        self.assertEqual(response.status_code, 422)
        self.assertTrue(self.job.live_stream.queue.empty())

    async def test_client_disconnect_cancels_job_but_holds_native_slot_until_acknowledged(self):
        request = Mock(); request.is_disconnected = Mock(side_effect=lambda: asyncio.sleep(0, result=False))
        stream = self.c._stream_chat_response(self.job, request, "test", self.job.created_at, False)
        pending = asyncio.create_task(anext(stream))
        await self.event()
        await asyncio.wait_for(pending, 1)
        await stream.aclose()
        self.assertTrue(self.job.cancelled)
        self.assertNotIn(self.job.id, self.c.jobs)
        self.assertTrue(self.c.machines[self.cluster.primary_id].busy)
        response = await self.submit(self.job, self.claimed["claim_token"])
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.c.machines[self.cluster.primary_id].busy)

    async def test_backpressure_stays_bounded_and_cancellation_unblocks_upload(self):
        for seq in range(8):
            self.assertEqual((await self.event(seq)).status_code, 200)
        blocked = asyncio.create_task(self.event(8))
        await asyncio.sleep(.1)
        self.assertFalse(blocked.done())
        self.assertEqual(self.job.live_stream.queue.qsize(), 8)
        self.c._cancel_job(self.job.id, "test")
        self.assertEqual((await asyncio.wait_for(blocked, 1)).status_code, 410)


class AgentStreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = load("streaming_agent", ROOT / "agents/agent.py")

    def test_stream_assembly_preserves_reasoning_multiple_tools_unicode_and_usage(self):
        chunks = [
            {"id": "completion", "choices": [{"delta": {"role": "assistant", "reasoning_content": "reason"}}]},
            {"choices": [{"delta": {"content": "café", "tool_calls": [{"index": 0, "id": "a", "function": {"name": "read", "arguments": '{"path":'}},
                                                                           {"index": 1, "id": "b", "function": {"name": "read", "arguments": '{"path":"b"}'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 8, "total_tokens": 11}}]
        response = Mock(iter_lines=Mock(return_value=[*(('data: ' + json.dumps(c)).encode() for c in chunks), b"data: [DONE]"]))
        emit = Mock(); result = self.agent._read_rpc_stream(response, emit)
        msg = result["choices"][0]["message"]
        self.assertEqual(msg["content"], "café")
        self.assertEqual(msg["reasoning_content"], "reason")
        self.assertEqual([json.loads(c["function"]["arguments"])["path"] for c in msg["tool_calls"]], ["a", "b"])
        self.assertEqual(result["usage"]["total_tokens"], 11)
        self.assertEqual(emit.call_count, 4)
        with self.assertRaisesRegex(RuntimeError, "before completion"):
            self.agent._read_rpc_stream(Mock(iter_lines=Mock(return_value=[])), Mock())

    def test_event_upload_retry_preserves_sequence_and_payload(self):
        session = Mock(); session.__enter__ = Mock(return_value=session); session.__exit__ = Mock(return_value=False)
        ack = Mock(status_code=200, json=Mock(side_effect=lambda: {"next_sequence": session.post.call_args.kwargs["json"]["sequence"] + 1}))
        session.post.side_effect = [self.agent.requests.Timeout(), ack, ack]
        with patch.object(self.agent.requests, "Session", return_value=session):
            publisher = self.agent.RpcEventPublisher("https://coordinator", "private", "job", "claim")
            publisher.emit({"choices": [{"delta": {"content": "hello"}}]})
            publisher.close()
        calls = session.post.call_args_list
        self.assertEqual(calls[0].kwargs["json"], calls[1].kwargs["json"])
        self.assertFalse(publisher.thread.is_alive())
