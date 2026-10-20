# Relay routing, preflight and recovery

Cloud relay use and provisioning are currently disabled. The provisioning host
service is stopped and disabled, static relay credentials/URL are cleared, and
`RPC_CLOUD_RELAY_ENABLED=false`. Terraform applied the destroy plan; the relay
module state is empty. AWS also reported no pending, running, stopping, stopped
or shutting-down instances with the relay project tags. The module and its
private inputs are retained at `infra/relay` for future use. Account region
activation is a separate, non-compute state and was not disabled.

## Route selection

`RPC_DEFAULT_TRANSPORT=auto` uses this order for a new two-machine model:

1. Direct LAN QUIC when both selected agents support it.
2. Configured local relay candidates covering every participating agent.
3. Configured or provisioned cloud relay candidates, only when cloud use is enabled.
4. The coordinator's existing outbound HTTP relay.

Address affinity orders candidate machine pairs; it is not proof of connectivity.
A route is proved by an end-to-end challenge before model download, native runtime
installation, Ollama shutdown or GPU model startup. A worker first runs a tiny
CPU-only echo listener. The primary sends a unique cluster challenge through the
actual selected bridge and requires the matching answer. The worker reports
verification, replaces the temporary listener with the native RPC server on a
fresh port, and reports native readiness. Only then can the primary load its model.
The fresh port avoids Windows TIME_WAIT/excluded-port problems. QUIC and HTTP
relay modes use this same check. Failed checks trigger teardown and route selection.

Agents advertise `runtime.rpc_preflight=true`. Automatic new pair formation
requires that capability. Older active roles remain running until their normal
teardown; safe updates install the new agent when idle. Explicit diagnostic
transports remain available. Administrator-pinned trials can use `quic` or `auto`
with two `rpc_agent_ids`; recovery retains those requested machine identities.

## Multiple relays and separate networks

`RPC_RELAYS` is a private JSON array in `.env`. It may contain up to 16 candidates:

```json
[
  {
    "id": "network-a",
    "scope": "local",
    "url": "https://relay-a.example",
    "control_key": "REPLACE_WITH_PRIVATE_CONTROL_KEY",
    "agent_ids": ["agent-a1", "agent-a2"]
  },
  {
    "id": "network-b",
    "scope": "local",
    "url": "https://relay-b.example",
    "control_key": "REPLACE_WITH_PRIVATE_CONTROL_KEY",
    "agent_ids": ["agent-b1", "agent-b2"]
  }
]
```

Use the existing `coordinator/relay_server.py` relay protocol with the agent key
and a distinct control key. The URL must use trusted HTTPS and be reachable from
the coordinator for control and from both selected agents for data. `agent_ids`
is an optional coverage restriction; the actual challenge must still pass.
The coordinator keeps independent relay state per cluster. A relay limited to
network A is never selected for a pair spanning networks A and B. Two replicas
on separate networks can use different relays simultaneously. Relays are not
chained together; a pair spanning networks needs one route reachable by both,
including the coordinator fallback.

Candidate registration is configuration-based. Automatic discovery/hosting of a
relay service on an arbitrary newly joined agent is not implemented. Direct LAN
QUIC already provides independent local connections without a dedicated relay
host or administrator access when peer UDP works.

## Recovery boundaries

Automatic clusters retain requests through route-check failures, failed model
processes, lost members and unavailable relays. Failed routes enter a five-minute
cooldown for the affected pair. New attempts prefer another ready replica or
select another allowed route. Old assignments must be released before a new
attempt starts; old claim tokens are fenced so late results cannot overwrite the
new attempt. Recovery keeps model, context and the original request deadline,
and allows at most three recoveries. Cancelled, completed or disconnected
requests are not revived. Offline/stale native processes remain ineligible for
new work through the existing physical-machine reservation checks.

A live native RPC session cannot migrate its socket state. Its model processes
must stop and reload on the replacement route. Inference checks assignment health
while running so teardown need not wait for the HTTP inference timeout. Relay
health detection retains the existing bounded heartbeat/lease intervals.

GPU/disk exhaustion, missing credentials and invalid model files may still require
operator action after retries fail. Coordinator HTTP connections and its
in-memory chat queue do not survive an unexpected coordinator crash; callers
must retry. This is bounded route recovery, not durable request execution.

## Re-enable a cloud relay later

The renamed module retains the small instance/default-VPC/budget/runtime checks.
No cloud resource is created by a preview or configuration command.

```sh
python3 scripts/configure-relay-provisioner.py --bind 172.17.0.1
python3 scripts/configure-relay-provisioner.py --bind 172.17.0.1 --apply
sudo install -m 0644 .rpc-trial/relay-provisioner/swarm-relay-provisioner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now swarm-relay-provisioner
# Deploy the coordinator after draining existing work.
```

The provisioner helper explicitly enables cloud use and retains `auto` routing. The bind
address must match this host's Docker gateway. For a manually deployed relay use
`scripts/configure-relay.py`; choosing that static endpoint explicitly selects
`relay` transport and enables cloud use. Its former entry point remains a
compatibility wrapper. Disable again with `scripts/configure-relay.py --disable --apply`, stop
and disable the service, deploy the cleared configuration, and run Terraform
destroy against `infra/relay`. Keep state and private inputs until cleanup is verified.
