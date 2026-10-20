# Agent snapshot

`agent.py` is an unchanged copy of the local `agent/agent.py` reviewed on
2026-10-09. That directory is a separate, ignored checkout of
[`unknowncoder05/swarm-llm-agent`](https://github.com/unknowncoder05/swarm-llm-agent).
Its base commit was `fcc2829b997679afc2d3c0223c25e668b5552aa4`, with local
working-tree changes including the automatic RPC implementation. This snapshot
is **not** the contents of that base commit or a claim that those changes have
been published upstream.

SHA-256 of the captured file:

```text
4063ce5d99ce59fb28c71b51cbfe831d53f446666d779c9d63bc44fa569bc8af
```

The lab uploads this file as `agent.py`, applies its adapters at process startup,
and disables agent self-updates. It preserves this source so a fresh main-project
checkout can reproduce the tested application code. To exercise another reviewed
agent, set `agent_source_path` to its local path; Terraform records the changed
content hash and replaces the instances. Keep the required automatic RPC methods
compatible with `scripts/launch.py`.
