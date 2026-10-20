"""API-key authentication, client identity and bounded request rate limits."""

from collections import defaultdict
import hmac
import time
from typing import Optional
from fastapi import HTTPException, Request, Security
from fastapi.security.api_key import APIKeyHeader

_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _ct_match(provided: Optional[str], *valid: str) -> bool:
    """Constant-time check: provided matches any of the valid keys."""
    if not provided:
        return False
    return any(hmac.compare_digest(provided.encode(), v.encode()) for v in valid)


def _extract_key(request: Request, header_key: Optional[str]) -> Optional[str]:
    """Accept key from X-API-Key header or Authorization: Bearer <key>."""
    if header_key:
        return header_key
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


class _RateLimiter:
    def __init__(self, max_calls: int, window_sec: int):
        self.max_calls = max_calls
        self.window_sec = window_sec
        self._log: dict[str, list[float]] = defaultdict(list)

    def check(self, ip: str) -> None:
        now = time.time()
        cutoff = now - self.window_sec
        calls = self._log[ip]
        while calls and calls[0] < cutoff:
            calls.pop(0)
        if len(calls) >= self.max_calls:
            raise HTTPException(
                429,
                f"Rate limit exceeded: max {self.max_calls} requests per {self.window_sec}s",
            )
        calls.append(now)


def _real_ip(request: Request) -> str:
    """Extract the real client IP. nginx sets X-Real-IP to $remote_addr."""
    forwarded = request.headers.get("X-Real-IP")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class ApiKeyAuth:
    def __init__(self, admin_key, agent_key, consumer_key):
        self.admin_key = admin_key
        self.agent_key = agent_key
        self.consumer_key = consumer_key

    def require_agent(self, request: Request, key: Optional[str] = Security(_header)):
        if not _ct_match(_extract_key(request, key), self.agent_key, self.admin_key):
            raise HTTPException(401, "Invalid or missing API key")
        return key

    def require_consumer(
        self, request: Request, key: Optional[str] = Security(_header)
    ):
        if not _ct_match(_extract_key(request, key), self.consumer_key, self.admin_key):
            raise HTTPException(401, "Invalid or missing API key")
        return key

    def require_admin(self, request: Request, key: Optional[str] = Security(_header)):
        if not _ct_match(_extract_key(request, key), self.admin_key):
            raise HTTPException(401, "Invalid or missing API key")
        return key
