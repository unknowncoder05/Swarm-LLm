# Agent failure reports

The Python LLM agent reports startup exceptions, unexpected runtime exceptions,
Ollama server exits, and failed on-demand model downloads through its existing
authenticated `POST /agent-status` connection. Startup failures are caught before
the process exits, so the Windows watchdog can restart it without losing the
error. Failed downloads retain the last 40 output lines and the process exit
code instead of returning only `Failed to pull`.

Each report contains a unique ID, host and agent identity, model, occurrence time,
exception and traceback, up to 16 KiB of output, and diagnostic information:
platform, configured Ollama executable/port, expected model directory, disk free
space on that directory's volume, detected GPU, agent/parent/Ollama process IDs,
supervision mode, and the revision of the running agent. The model directory comes
from `OLLAMA_MODELS` or the current user's default Ollama store. If an externally
managed Ollama server uses another account or store, this directory and its
free-space reading may differ from that server's actual storage.

Known agent/Hugging Face credentials and signed download URL query strings are
redacted before transmission. Coordinator credentials are also redacted before
reports are stored. Reports contain operational error logs, so reads require the
admin key; consumer and agent keys cannot read them.

The agent writes reports to `%LOCALAPPDATA%\CodeSwarm\SwarmLLM\reports` on Windows
(under `~/CodeSwarm/SwarmLLM` when `LOCALAPPDATA` is absent). It keeps up to 20
undelivered reports and retries delivery on status updates and restart. A failed
HTTP response leaves the report queued. The coordinator deduplicates retransmits
by report ID and persists the latest 1,000 reports in its existing SQLite data
volume. Normal setup progress and successful registration never erase reports.

Expand **Last failure** under the machine name in the dashboard to inspect its
most recent report, including for registered machines. This is historical error
evidence: a live machine can still show an earlier failure. The full retained
history remains available even if a machine disappears from the dashboard:

```http
GET /swarm/api/agent-reports?hostname=A506-BIBLIO-08&limit=50
X-API-Key: <admin key>
```

Optional filters are `hostname` and `agent_id`; `limit` is clamped to 1–100.
Submitting an error requires an agent or admin key. Reports larger than 64 KiB
are rejected; traceback and log-tail fields each have a 16,384-character limit.
Existing agents that send `CRASH:` or `Pull failed` status details also have
those messages retained, though they cannot supply details they already omitted.

## Updating an agent that fails during setup

The new Python agent checks for updates after coordinator authentication and
before installing Ollama or pulling its initial model, as well as during normal
operation. Older agents only check after successful setup; a machine stuck in
an old startup crash loop needs the current script downloaded and launched once.
Use the existing connection command from the dashboard. Its saved context avoids
having to re-enter credentials. An unmodified old agent cannot be instructed to
upload local files through the coordinator's current outbound polling protocol.

Python exceptions and subprocess failures are covered. Hard termination, power
loss, errors before credentials are configured, and dependency bootstrap failures
before the agent imports cannot be reported through this path. The legacy
PowerShell worker is not the Python worker and retains its existing reporting
behavior; use the dashboard's Python connection command for the full reports.

## Validation

```sh
uv run --python 3.12 --with-requirements coordinator/requirements.txt \
  --with requests --with httpx python tests/test_agent_reports.py
cd ui
npm run build
```

The regression tests cover pull error capture, startup exception reporting,
Windows `cp1252` output with Ollama's Unicode progress spinner,
credential redaction, offline replay across agent restarts, HTTP delivery failure,
SQLite persistence across coordinator reinitialization, retention after progress
and registration, retransmit deduplication, authenticated access, and report size
limits.
