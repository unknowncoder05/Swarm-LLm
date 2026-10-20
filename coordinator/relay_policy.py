"""Route ordering and pair-specific failure cooldowns, independent of GPU startup."""
from dataclasses import dataclass
import json
import time
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Route:
    identity: str
    transport: str
    scope: str
    url: str = ""
    control_key: str = ""
    agent_ids: frozenset = frozenset()


class RelayPolicy:
    def __init__(self, raw="[]", cloud_enabled=False, clock=time.time, cooldown=300):
        self.clock, self.cooldown = clock, cooldown
        self.cloud_enabled = cloud_enabled
        self.failed = {}
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) > 16:
            raise ValueError("RPC_RELAYS must be an array of at most 16 relay candidates")
        self.routes = []
        identities = {"lan-quic", "coordinator"}
        for item in values:
            identity = item.get("id", "")
            parsed = urlsplit(item.get("url", ""))
            scope = item.get("scope", "local")
            allowed = item.get("agent_ids", [])
            if (not identity or len(identity) > 64 or identity in identities or scope not in ("local", "cloud")
                    or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or not item.get("control_key")
                    or not isinstance(allowed, list) or len(allowed) > 64
                    or any(not isinstance(a, str) or not a for a in allowed)):
                raise ValueError("Invalid relay candidate; require unique ID, HTTPS URL and control key")
            identities.add(identity)
            if scope == "cloud" and not cloud_enabled:
                continue
            self.routes.append(Route(identity, "relay", scope, item["url"].rstrip("/"),
                                     item["control_key"], frozenset(allowed)))
        self.routes.sort(key=lambda r: r.scope != "local")

    def candidates(self, agent_ids, quic=True, managed=False):
        members = frozenset(agent_ids)
        routes = ([Route("lan-quic", "quic", "local")] if quic else [])
        routes += [r for r in self.routes if not r.agent_ids or members <= r.agent_ids]
        if managed and self.cloud_enabled:
            routes.append(Route("managed-relay", "relay", "cloud"))
        routes.append(Route("coordinator", "relay", "coordinator"))
        now = self.clock()
        self.failed = {key: until for key, until in self.failed.items() if until > now}
        return [r for r in routes if self.failed.get((r.identity, members), 0) <= now]

    def reject(self, identity, agent_ids):
        self.failed[(identity, frozenset(agent_ids))] = self.clock() + self.cooldown

    def status(self):
        return [{"id": r.identity, "scope": r.scope} for r in self.routes]
