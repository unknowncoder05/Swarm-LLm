# Agent LAN connectivity probe

## Required relay priority

For a new multi-machine cluster, use this order:

1. A working same-network relay, verified reachable by every participating agent.
2. The relay, provisioned on demand with Terraform when no suitable LAN
   relay is available.
3. The existing coordinator relay when the first two options are unavailable.

Keep the chosen route for an active cluster. A failed route can trigger a new
cluster after its old processes and reservations are safely released; it cannot
transparently migrate live native RPC sockets. Do not provision Spain while a
working LAN relay can serve the selected machines.

The [current routing policy](relay-routing.md) adds end-to-end preflight and
bounded failover. Cloud relay use is currently disabled.

The diagnostic probe below supplies LAN reachability evidence only. It does not install a
production LAN relay or implement automatic coordinator route selection. That
integration must also handle the LAN relay's control plane: a remote coordinator
cannot directly call the library machine's private IP, so registration/renewal
needs the host agent's outbound channel.

## Run the connectivity test

The Python agent can test direct TCP connectivity between two installed agents
without administrator privileges, firewall changes, GPU work, or coordinator
restarts. Run this on the coordinator host:

```sh
python3 scripts/agent-lan-probe.py --hosts GPU-NODE-01 GPU-NODE-02
python3 scripts/agent-lan-probe.py --apply --hosts GPU-NODE-01 GPU-NODE-02
```

The first command previews the two selected agents. The second waits for their
normal idle updater to install the current source, then publishes a temporary
`agent/lan-probe.json` through the existing script-download route. The trigger
contains two selected agent IDs, private LAN addresses, a non-secret random
handshake nonce, and a lifetime of at most 180 seconds. It contains no API keys.
The controller removes it after the trial; agents also reject expired triggers.

Each agent binds its selected LAN interface to an OS-assigned available port.
Once both endpoints are known, each connects to the other and verifies the
handshake. Three bounded attempts are allowed per direction. Listeners accept
only the selected peer address, read at most 128 bytes, accept at most 16
connections, and close automatically. The probe supports no shell commands,
file uploads, model changes, or arbitrary public destinations.

The agent saves evidence in its current-user context directory as
`lan-probe-result.json`. Expected connection failures are diagnostic results,
not crash reports. While idle, it publishes compact `LAN_PROBE` status details
for the controller to collect into a private `.rpc-trial/*-lan-probe/result.json`.
It does not overwrite running chat, media, or RPC status. Automatic updates wait
for an active probe to finish. Existing model jobs are never stopped to install
or run this test, so a warm RPC cluster can delay its start.

Passing both directions establishes reachability on the selected ports at that
time. If only one direction passes, its destination is a potential relay host
for this pair; the controller reports that host in `relay_candidate_ids` and
marks the trial `partial`. A relay needs incoming access from its clients, so
the two directions do not need to have identical firewall behavior.
The probe does not measure bulk throughput, prove every port is allowed, or
establish production relay reliability. A failed test cannot by itself identify
whether Windows firewall rules, network isolation, or another connectivity issue
caused the failure.

Six agent tests cover real bidirectional socket handshakes and closure, invalid
and expired plans, refusal/mismatched services, persisted results during RPC,
and automatic-update gating. During the isolated full-suite check, 200 tests
passed; an independent provisioning test failed before invoking any agent code
because `TerraformRelay` tried to create a log directory under a missing parent
directory (`test_missing_expired_instance_is_recreatable_but_auth_errors_are_not_hidden`).
The provisioning implementation was left untouched by this diagnostic change.

## UDP alternative without administrator access

If TCP fails, test simultaneous UDP traffic from both agents:

```sh
python3 scripts/agent-lan-probe.py --apply --transport udp --hosts GPU-NODE-01 GPU-NODE-02
```

This uses the same safe-update wait, temporary manifest, selected private IPv4
addresses, status reporting, expiry and cleanup as the TCP test. Each agent binds
one OS-assigned UDP port and uses that same socket for outgoing probes and
incoming replies. Once both ports are published, both agents send challenge
packets for 15 seconds. This tests whether simultaneous outgoing traffic permits
peer responses without an inbound firewall exception. It does not change firewall
rules, install a VPN, or discover public/NAT endpoints with STUN.

An agent sends at most 24 probes and 32 acknowledgements, processes at most 128
datagrams, and accepts packets of at most 128 bytes from the exact selected peer
address and advertised port. A successful reply must match this run's token and
an outstanding random challenge. Evidence includes sent/received counts and
handshake round-trip times; these are not bulk-throughput measurements.

UDP success is reported separately as `direct_udp_verified`, with no TCP
`relay_candidate_ids`. The current native RPC bridge carries TCP over HTTP;
working UDP would still need a reliable, ordered transport adapter before it
could carry inference. Failed UDP does not establish the precise firewall or
network-isolation cause. It leaves the relay as the existing alternative.

Twelve focused tests cover the original TCP path plus real simultaneous UDP
handshakes, socket cleanup, silent peers, denied socket creation, and rejection
of wrong sources, tokens, challenges and oversized packets. UDP evidence cannot
accidentally nominate a TCP relay host.
