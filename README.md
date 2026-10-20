# Swarm LLM

Dashboard: https://sandbox.yerson.co/swarm/

See the [architecture and component guide](docs/architecture.md) for scheduling,
inference transports, streaming, persistence, deployment and repository ownership.

The [controller status guide](docs/controller-dashboard.md) explains loaded
models, distributed replicas, download progress and GPU memory reporting.
The Chat tab [waits for distributed agents after restarts](docs/ui-startup-wait.md)
and continues through model setup without resubmitting accepted inference.

Cloud relay use is currently disabled and its resources are removed. See
[relay routing and recovery](docs/relay-routing.md) for connectivity checks before
model startup, network-specific relay candidates, bounded failover and re-enabling.

The gateway binds to `127.0.0.1:8081`. The coordinator (`8080`) and UI
(`8082`) are only exposed inside Docker. Host ports 8000 and 3000 remain
available for the other application.

Start or rebuild the full stack with `make rebuild-all`; inspect it with
`make status` and `make health`. Containers restart automatically unless stopped.
Existing API keys remain in `.env`; use the admin key to sign into the dashboard.

Public endpoints:

- Agent coordinator: `https://sandbox.yerson.co/swarm/api`
- OpenAI-compatible API: `https://sandbox.yerson.co/swarm/api/v1`
- Health: `https://sandbox.yerson.co/swarm/api/healthz`

## OpenCode

Configure and launch the distributed model:

```sh
python3 scripts/setup-opencode.py
python3 scripts/run-opencode.py
```

If OpenCode is missing, add `--install` to setup to install it locally using npm,
without administrator access. On Windows use `py -3` instead of `python3`.
Setup accepts your Swarm URL and consumer key, shows the live model choices,
and preserves other providers and settings. In OpenCode use `/models`, select a
Swarm model, and send a prompt; Swarm downloads/starts it on demand.
Use `python3 scripts/setup-opencode.py --list-models` to inspect the current
catalog without starting inference. See [client setup](docs/opencode-setup.md)
for refreshing the picker, switching models, and cold-start behavior.
The launcher loads the credential privately from the environment, `.env.opencode`,
or `.env`. The generated
30B profile requests a 32K context for coding tools and keeps reasoning enabled.
Use `python3 scripts/opencode-smoke.py` for an isolated read/edit/test trial.
Updated distributed agents support incremental responses; see
[30B latency tuning and streaming](docs/performance.md) for the
controlled cache/batch benchmark and `scripts/rpc-stream-smoke.py`.

For machine joins, model placement, workload contention, and recovery behavior,
see [scheduling and resource ownership](docs/architecture.md#scheduling-and-resource-ownership)
and [agent failure reporting](docs/agent-failure-reports.md).
The [on-demand relay](docs/relay-provisioning.md) supports coordinator-triggered
Terraform provisioning, bounded instance profiles, lease renewal, and idle cleanup.
The [queue durability design](docs/queue-durability.md) evaluates RabbitMQ and
the state/lease changes needed before adopting it.

Dated trial results and work journals are retained locally in ignored `.journal/`.
The tracked guides describe supported behavior, configuration and verification.

## Distributed models and AWS trial

The Python agents can combine GPUs on multiple machines through llama.cpp RPC.
The coordinator forms a cluster when a client requests a model in its RPC
catalog. The primary connects directly to the workers over a private network and
pulls a missing GGUF into its Ollama cache automatically.

When peer UDP works, [direct LAN QUIC](docs/direct-quic-inference.md) carries
reliable, encrypted RPC between two Python agents without administrator access
or an external relay. Automatic routing tries it first and verifies connectivity
before model startup; see [routing and recovery](docs/relay-routing.md).
Request `"rpc_transport": "quic"` to require that transport explicitly.
Independent GPU pairs can serve the same model through separate replicas;
[the architecture guide](docs/architecture.md) covers admission, routing and
preservation of idle native clusters during coordinator deployment.

For machines without a working direct LAN path, set `"rpc_transport": "relay"` on the
first chat request. Both Python agents connect outward over HTTPS through the
existing coordinator, with native RPC listeners on localhost. This needs no
Windows administrator privileges but adds transfer time and network latency.
Run `python3 scripts/rpc-trial.py --transport relay` for a two-machine GPU trial.
For nearby forwarding using the existing GPUs, the [relay module](infra/relay/README.md)
creates one small CPU instance in the default VPC with Terraform and a bounded
trial lifetime.

See [distributed inference](docs/distributed-inference.md) for the architecture,
requirements, and known limitations. A self-contained, disposable AWS experiment
with two small real GPUs (or a larger Spot profile) is in [infra/rpc-lab](infra/rpc-lab/README.md). Terraform
owns its cloud resources; the trial runner destroys them after the experiment.

## Windows LLM agent

Run the downloaded `agent.py` normally. On its first successful configuration it
stores the coordinator, credential, selected model, and Ollama port under the
current user's `%LOCALAPPDATA%\\CodeSwarm\\SwarmLLM` directory. It then installs
and starts a hidden watchdog through the current user's Startup folder. Future
logins, coordinator outages, crashes, and self-updates are handled automatically;
no administrator permission or manual Docker/Ollama context selection is needed.
The dashboard's explicit coordinator/key command also starts the watchdog
immediately. Use `--foreground` only when you want to keep the agent attached
to the terminal for troubleshooting.

Running `agent.py` again without arguments reuses the saved context and repairs
the watchdog. Runtime output is kept in
`%LOCALAPPDATA%\\CodeSwarm\\SwarmLLM\\agent.log` instead of opening a terminal.
The stable agent ID is kept with this context, with migration from the older
TEMP file. Newly started Ollama servers listen on localhost; an adopted existing
server keeps its existing listener configuration.

Startup and runtime failures are reported automatically. Expand **Last failure**
on a machine in the dashboard to see the traceback, Ollama output, exit code,
and disk-space details. Reports survive retries and coordinator restarts; an
admin can also query `GET /swarm/api/agent-reports?hostname=MACHINE_NAME` with
the admin key. See [agent failure reports](docs/agent-failure-reports.md).

Check installation health, update consistency, API permissions, and inference:

```sh
python3 scripts/agent-health.py --hosts A506-209-12 A506-209-13 --smoke
```

The check waits for current supervised agents, observes which agents claim the
short inference jobs, verifies at least 80% GPU placement according to Ollama's
loaded-model telemetry, and saves private evidence under `.rpc-trial/`. Omit
`--smoke` for health and permission checks without model execution.

## Container jobs

The optional container worker turns registered machines with Docker into a small,
pull-based batch cluster. The coordinator never connects to a worker, so the same
NAT/firewall model as the LLM agent applies.

Install Python 3, `requests`, and Docker on a worker, then run:

```bash
python3 -m pip install requests
export SWARM_AGENT_KEY='the agent key'
python3 container_agent.py --coordinator https://sandbox.yerson.co/swarm/api
```

Queue a job with the admin key. `image` is an OCI/Docker registry reference (a
tag or, preferably, an immutable digest), and artifact paths are relative to the
container's `/workspace` directory:

```bash
curl -X POST https://sandbox.yerson.co/swarm/api/container/jobs \
  -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{
    "image": "alpine:3.20",
    "command": ["sh", "-c", "echo hello > result.txt"],
    "artifacts": ["result.txt"],
    "timeout_sec": 300,
    "cpus": 1,
    "memory_mb": 256
  }'
```

Use `GET /container/jobs`, `GET /container/jobs/{id}`, and
`GET /container/jobs/{id}/artifacts` to inspect jobs and download the resulting
`.tar.gz`. Network access is disabled unless a job explicitly sets `network` to
`true`. Workers also enforce a read-only root filesystem, drop Linux capabilities,
set `no-new-privileges`, and apply CPU, memory, PID, and wall-clock limits. This is
useful batch isolation, but it is not a hostile multi-tenant security boundary;
run untrusted workloads on dedicated worker hosts or stronger VM sandboxes.

Agents and clients previously configured with `/api` must use `/swarm/api`.
The HTTPS nginx server routes `/swarm/` to the gateway without stripping the
prefix. See `nginx/sandbox-swarm.conf.example` for the required locations.
