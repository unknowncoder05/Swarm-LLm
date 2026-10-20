# Reviewed agent sources

The portable source tree includes the Python agent, QUIC helper, PowerShell
launcher and container worker in `agents/`. Compose mounts that directory
read-only into both the coordinator and gateway. Public agent downloads and the
authenticated source endpoint therefore serve the same reviewed files.

The Python agent pins the QUIC helper by SHA-256. Deploy the agent and helper
together after regression checks; active jobs and roles gate self-updates.

| File | SHA-256 |
| --- | --- |
| `agents/agent.py` | `8a121ebf5784fdc0fd9e036d50ed953a501ce4431b900ad4926ee6f1bacdde46` |
| `agents/quic_transport.py` | `cba1de540348721c8dfba8a4aad1cc9243662e525fbd978beadb30136b4a8fc9` |

The AWS lab intentionally uses its separate captured snapshot and adapters. Its
source hash is documented alongside that snapshot. A deployment without a mounted
source may explicitly configure `AGENT_SOURCE_URL`; no personal upstream
repository is fetched by default. Missing configured source returns HTTP 503.

See [architecture](architecture.md) and [performance checks](performance.md).
