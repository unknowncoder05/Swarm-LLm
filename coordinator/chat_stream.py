"""Bounded, ordered agent events for OpenAI streaming with claim fencing."""

import asyncio
import hashlib
import json
import hmac
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

try:
    from validation import validate_contract
except ModuleNotFoundError:
    from coordinator.validation import validate_contract

MAX_BATCH_BYTES = 64 * 1024


class StreamBuffer:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=8)
        self.sequence = -1
        self.digest = ""
        self.lock = asyncio.Lock()


class EventBatch(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    claim_token: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=0, le=262144, strict=True)
    chunks: list[dict] = Field(min_length=1, max_length=64)


def delta_chunks(job, chunks):
    """Forward deltas; commit finish/usage only with the final fenced result."""
    validate_contract("stream-chunks", chunks)
    output = []
    for chunk in chunks:
        choices = chunk.get("choices", [])
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta", {})
        clean = {
            k: v
            for k, v in delta.items()
            if k in ("role", "content", "reasoning_content", "tool_calls")
        }
        if clean:
            output.append(
                {
                    "id": "chatcmpl-" + job.id,
                    "object": "chat.completion.chunk",
                    "created": int(job.created_at),
                    "model": job.model,
                    "choices": [{"index": 0, "delta": clean, "finish_reason": None}],
                }
            )
    return output


def install_stream_routes(app, jobs, require_agent):
    @app.post("/agent/jobs/{job_id}/events")
    async def events(job_id: str, request: Request, _key=Depends(require_agent)):
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > MAX_BATCH_BYTES:
                raise HTTPException(413, "Stream batch exceeds 64 KiB")
        try:
            payload = EventBatch.model_validate_json(bytes(raw))
        except ValueError:
            raise HTTPException(422, "Invalid stream batch") from None
        job = jobs.get(job_id)
        if not job or job.cancelled or job.done_event.is_set():
            raise HTTPException(410, "Streaming job is no longer available")
        buffer = getattr(job, "live_stream", None)

        def current():
            return (
                jobs.get(job_id) is job
                and not job.cancelled
                and not job.done_event.is_set()
                and not job.rpc_recovering
                and job.assigned_agent_id == payload.agent_id
                and job.claim_token
                and hmac.compare_digest(job.claim_token, payload.claim_token)
            )

        if buffer is None or not current():
            raise HTTPException(409, "Stream batch belongs to an expired claim")
        digest = hashlib.sha256(raw).hexdigest()
        async with buffer.lock:
            if not current():
                raise HTTPException(409, "Stream claim changed")
            if payload.sequence == buffer.sequence and digest == buffer.digest:
                return {"next_sequence": buffer.sequence + 1}
            if payload.sequence != buffer.sequence + 1:
                raise HTTPException(
                    409, "Stream batch is duplicated, changed or out of order"
                )
            try:
                chunks = delta_chunks(job, payload.chunks)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            # Wait for the consumer with a bounded memory footprint. Check claim
            # fencing again after backpressure releases the event loop.
            until = asyncio.get_running_loop().time() + 10
            while buffer.queue.full():
                if not current():
                    raise HTTPException(410, "Stream cancelled during backpressure")
                if asyncio.get_running_loop().time() >= until:
                    raise HTTPException(503, "Stream consumer is too slow")
                await asyncio.sleep(0.05)
            if not current():
                raise HTTPException(410, "Stream cancelled")
            if chunks:
                buffer.queue.put_nowait(chunks)
                job.stream_started = True
            buffer.sequence = payload.sequence
            buffer.digest = digest
        return {"next_sequence": buffer.sequence + 1}


def sse(value):
    return "data: " + json.dumps(value) + "\n\n"
