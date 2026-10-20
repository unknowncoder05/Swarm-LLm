"""Isolated coordinator fixtures shared by scheduling scenario suites."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from test_agent_reports import ROOT, load


class SchedulingFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"ADMIN_KEY": "admin", "AGENT_KEY": "agent", "CONSUMER_KEY": "consumer",
                                     "RPC_RELAY_URL": "", "RPC_RELAY_ADMIN_KEY": ""}):
            self.c = load("scheduling_coordinator", ROOT / "coordinator/coordinator.py")
        self.c.DB_PATH = str(Path(self.temp.name) / "db.sqlite")
        self.c._init_db()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.c.app), base_url="http://test")
        self.headers = {"X-API-Key": "agent"}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    def machine(self, name, vram=12, **extra):
        registration = self.c.RegisterPayload(agent_id=name, hostname=name, port=11434, model="qwen3:14b",
            gpu_vram_gb=vram, cpu_cores=8, ram_total_gb=32, lan_ip=f"10.0.0.{len(self.c.machines) + 1}", **extra)
        machine = self.c.MachineState("same-nat", registration)
        self.c.machines[name] = machine
        return machine

    def enqueue(self, model="qwen3:14b", context=2048):
        job = self.c.Job({"model": model, "messages": [{"role": "user", "content": "hello"}],
                          "options": {"num_ctx": context}}, model)
        self.c.jobs[job.id] = job
        self.c.job_queues.setdefault(model, asyncio.Queue()).put_nowait(job)
        return job

    async def claim(self, name, model="qwen3:14b"):
        return await self.client.get("/agent/jobs/next", params={"agent_id": name, "model": model}, headers=self.headers)

    async def submit(self, job, token, content="OK"):
        return await self.client.post(f"/agent/jobs/{job.id}/result", headers=self.headers,
            json={"claim_token": token, "result": {"choices": [{"message": {"content": content}}]}})
