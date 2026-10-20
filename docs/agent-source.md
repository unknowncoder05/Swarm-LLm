# Agent source for this release

The coordinator serves its agent from an ignored separate checkout at `agent/`.
The distributed inference release is committed there as:

```text
Repository: git@github.com:unknowncoder05/swarm-llm-agent.git
Commit:     a86a8d8699c5b57d1d4cb947a5b2e567b1bd5267
```

This records a local commit; it does not assert that the commit has been pushed
upstream. Retain this checkout or transfer its commit when deploying elsewhere.
To transfer the committed source without publishing it:

```sh
git -C agent bundle create /tmp/swarm-llm-agent.bundle main
# On the deployment host, from the coordinator checkout:
git clone /path/to/swarm-llm-agent.bundle agent
git -C agent checkout a86a8d8699c5b57d1d4cb947a5b2e567b1bd5267
```

Use an existing checkout instead of cloning over one. The expected source hashes
are:

| File | SHA-256 |
| --- | --- |
| `agent/agent.py` | `4f2bfba574f16ad8f5eff5947eb82b17b2e784e478b193345f056058b815f494` |
| `agent/quic_transport.py` | `cba1de540348721c8dfba8a4aad1cc9243662e525fbd978beadb30136b4a8fc9` |

The Python agent pins the QUIC helper's hash. Deploy both files together. Compose
mounts this directory read-only into the coordinator, and the gateway serves the
same directory. The reviewed source must be present before starting the stack.
The lab's October 9 snapshot intentionally keeps its own version and hash.

See [architecture](architecture.md) for the component boundaries and
[performance checks](performance.md) for verification commands.
