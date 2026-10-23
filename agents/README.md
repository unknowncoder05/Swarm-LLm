# peerfoundry agents

This repository contains two pull-based workers for the peerfoundry coordinator:

- `agent.py` serves Ollama chat and media inference jobs.
- `container_agent.py` runs finite Docker/OCI batch jobs and returns their logs,
  exit status, timing, and selected artifacts.

Both workers initiate outbound connections to the coordinator. The controller
does not need direct network access to worker machines, and workers do not need
inbound firewall rules.

## Distributed Python agent

The current `agent.py` also supports coordinator-assigned llama.cpp RPC primary
and worker roles. It installs the pinned native runtime, resolves/pulls GGUF
weights, reserves its workload slot, and reports native readiness and teardown.
Updated workers advertise route-preflight, QUIC, tuning and streaming capabilities.

`quic_transport.py` carries native RPC between exactly two capable private-IPv4
agents over reliable encrypted LAN UDP. The primary verifies the worker's exact
cluster certificate and each stream's cluster token. Both peers use bounded
buffers, and native listeners remain on loopback. The Python agent verifies the
downloaded helper's pinned SHA-256. Direct TCP and outbound HTTPS relay modes
remain available when selected by the coordinator.

Streamed RPC jobs publish ordered reasoning, text and tool-call batches over the
existing authenticated API, with bounded asynchronous upload and claim fencing.
Final results still use the normal completion path. Consumer cancellation stops
native inference through role teardown before releasing the GPU reservation.
Allowlisted runtime profiles adjust batch/microbatch and RAM snapshots; they do
not change the requested model or context.

Windows installation, saved configuration, watchdog startup, retained failure
reports and idle self-updates run under the current user without administrator
access. `enable-rpc-firewall.ps1` is an optional administrator-only helper for
direct TCP; QUIC and outbound relay operation do not require it.
`llama-rpc.ps1` is a separate legacy manual launcher, not the automatic Python path.

These sources are versioned with the coordinator. Deploy the Python source
and pinned QUIC helper together. The project's
`docs/architecture.md`, `docs/direct-quic-inference.md`,
`docs/agent-failure-reports.md` and `docs/performance.md` document the
control protocol, lifecycle and measured validation.

## Container worker

### Requirements

- Python 3.8 or newer
- The Python `requests` package
- A running Docker daemon accessible to the account running the worker
- An agent API key issued by the coordinator operator

The worker supports Linux containers on Linux Docker hosts and Windows
containers on Windows Docker hosts. A worker only leases jobs whose `platform`
matches the platform reported by its Docker daemon.

### Linux installation

```bash
sudo install -d /opt/peerfoundry
sudo curl -fsS \
  https://peerfoundry.example.com/peerfoundry/agent/container_agent.py \
  -o /opt/peerfoundry/container_agent.py
sudo python3 -m pip install requests
```

Create `/etc/peerfoundry-container-agent.env`:

```text
PEERFOUNDRY_AGENT_KEY=replace-with-the-agent-key
```

Install and enable the included service definition after checking its
coordinator URL and script path:

```bash
sudo cp container-agent.service.example /etc/systemd/system/peerfoundry-container-agent.service
sudo chmod 600 /etc/peerfoundry-container-agent.env
sudo systemctl daemon-reload
sudo systemctl enable --now peerfoundry-container-agent
sudo journalctl -u peerfoundry-container-agent -f
```

### Windows installation

Use a Windows Server host configured for Windows containers. The AWS Windows
Server 2022 ECS-optimized AMI is a convenient option because its container
runtime is preinstalled.

From an elevated PowerShell session:

```powershell
New-Item -ItemType Directory -Force C:\peerfoundry | Out-Null
Invoke-WebRequest `
  "https://peerfoundry.example.com/peerfoundry/agent/container_agent.py" `
  -OutFile C:\peerfoundry\container_agent.py
py -m pip install requests
$env:PEERFOUNDRY_AGENT_KEY = "replace-with-the-agent-key"
py C:\peerfoundry\container_agent.py `
  --coordinator https://peerfoundry.example.com/peerfoundry/api
```

For unattended operation, run that command as a Windows scheduled task under
`SYSTEM`. Store the agent key in a protected environment file or secret store;
do not embed it in a container job.

### Direct invocation

```bash
export PEERFOUNDRY_AGENT_KEY='replace-with-the-agent-key'
python3 container_agent.py \
  --coordinator https://peerfoundry.example.com/peerfoundry/api \
  --poll-seconds 3
```

The worker creates a stable identifier in
`~/.peerfoundry/container-agent-id`, registers as model `container-runtime`, and
sends heartbeats every 15 seconds.

## Submitting a container job

Container jobs are created through the coordinator, not through the worker.
The endpoint requires the coordinator admin key.

```bash
curl -X POST https://peerfoundry.example.com/peerfoundry/api/container/jobs \
  -H "X-API-Key: $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "image": "alpine:3.20",
    "command": ["sh", "-c", "echo hello > result.txt"],
    "artifacts": ["result.txt"],
    "timeout_sec": 300,
    "cpus": 1,
    "memory_mb": 256,
    "network": false,
    "platform": "linux/amd64"
  }'
```

Windows example:

```json
{
  "image": "mcr.microsoft.com/windows/nanoserver:ltsc2022",
  "command": ["cmd.exe", "/d", "/s", "/c", "echo hello>result.txt"],
  "artifacts": ["result.txt"],
  "timeout_sec": 600,
  "cpus": 1,
  "memory_mb": 512,
  "network": false,
  "platform": "windows/amd64"
}
```

The container runs with `/workspace` as its working directory on Linux and
`C:/workspace` on Windows. Artifact patterns are relative to that directory and
may contain recursive glob patterns such as `reports/**/*.json`.

Inspect and download the result with:

```bash
curl -H "X-API-Key: $ADMIN_KEY" \
  https://peerfoundry.example.com/peerfoundry/api/container/jobs/JOB_ID

curl -H "X-API-Key: $ADMIN_KEY" \
  https://peerfoundry.example.com/peerfoundry/api/container/jobs/JOB_ID/artifacts \
  -o artifacts.tar.gz
```

The artifact download is a gzip-compressed tar archive. If a job requests
artifacts but none match, the worker reports the job as failed instead of
silently returning an empty result.

## Execution and security model

Each attempt receives a fresh named Docker volume and container. The worker
pulls the requested image, applies the requested CPU, memory, and wall-clock
limits, captures the final 64 KiB of stdout and stderr, uploads matching
artifacts, and removes the temporary container and volume.

Network access defaults to disabled. Linux workloads additionally use a
read-only root filesystem, a 512-process limit, dropped capabilities,
`no-new-privileges`, and a restricted temporary filesystem. Windows Nano Server
jobs run as `ContainerAdministrator` inside the container so they can write to
the named workspace volume; this does not grant administrator rights on the
host.

This provides practical batch-job isolation, not a hostile multi-tenant
security boundary. Run untrusted images on dedicated hosts or inside stronger
VM/microVM isolation. Prefer immutable image digests, keep Docker and the host
patched, do not expose the Docker API over TCP, and do not place secrets in job
environment variables unless the coordinator database is protected accordingly.

## Verified AWS path

The Windows path was exercised end to end on a `t3.medium` EC2 instance using
the Windows Server 2022 ECS-optimized AMI. A Nano Server LTSC 2022 job wrote
`result.txt`; the worker uploaded a tar archive and the coordinator returned the
verified content `peerfoundry-windows-e2e-ok` with exit code `0`.
