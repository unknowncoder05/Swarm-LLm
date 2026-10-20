"""Async coordinator client for the private Terraform relay provisioner."""
import asyncio
import json
import time
import urllib.request
from urllib.parse import urlsplit


class RelayProvisionerClient:
    def __init__(self, url, key):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or not key:
            raise RuntimeError("Relay provisioner requires a private HTTP(S) URL and dedicated key")
        self.url, self.key = url.rstrip("/"), key
        self.lock = asyncio.Lock()

    def request(self, path, method="POST", timeout=900):
        req = urllib.request.Request(self.url + path, method=method,
            data=b"{}", headers={"X-API-Key": self.key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)

    async def ensure(self):
        async with self.lock:
            deployment = await asyncio.to_thread(self.request, "/ensure")
            parsed = urlsplit(deployment.get("url", ""))
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or not deployment.get("control_key")
                    or not isinstance(deployment.get("expires_at"), (int, float))
                    or deployment["expires_at"] <= time.time() + 300):
                raise RuntimeError("Provisioner did not return a healthy bounded relay deployment")
            return deployment

    async def renew(self, cluster_id):
        await asyncio.to_thread(self.request, "/leases/" + cluster_id, "PUT", 5)
