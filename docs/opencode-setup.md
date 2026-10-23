# Set up OpenCode for PeerFoundry

From a checkout of this repository, with Python 3 installed:

```sh
python3 scripts/setup-opencode.py --install
python3 scripts/run-opencode.py
```

Windows PowerShell:

```powershell
py -3 scripts/setup-opencode.py --install
py -3 scripts/run-opencode.py
```

`--install` requires Node.js/npm and installs OpenCode **1.18.32** beneath this
checkout's ignored `.opencode-tools/` directory. It uses no global npm prefix,
administrator permissions or cloud resources. Omit `--install` to use an existing
OpenCode installation. `make opencode-setup` and `make opencode` are equivalent
installation/launch entry points on systems with Make.

Setup asks for the API base URL and consumer key when needed, performs an
authenticated `GET /v1/models`, and prints the advertised models. Select the
default by number or model ID. Enter keeps the saved selection; this checkout
defaults to `qwen3:14b`. The chosen model is used for both normal and background requests.
The 30B A3B profile retains its 32,768-token context and reasoning; rerunning setup
preserves a previously chosen smaller context unless `--context` is supplied.
Existing providers,
tools and permission settings are retained; existing provider filters are adjusted
to allow `peerfoundry`. Malformed JSON and a competing `opencode.jsonc` stop setup
without replacing either file. Global OpenCode configuration is not written.

This follows OpenCode's official [custom provider](https://opencode.ai/docs/providers/#custom-provider)
and [configuration](https://opencode.ai/docs/config/) interfaces. Global and project
configurations are also merged by OpenCode itself; settings in the project being
opened can override this checkout's custom profile.

## Choosing and running models

1. Run setup, supply your PeerFoundry API URL (including `/v1`) and consumer key, and
   choose a model from the list.
2. Start the launcher. In OpenCode type `/models` to see/select the PeerFoundry models.
3. Send your coding request. The first inference request triggers any missing
   model download and startup on eligible agents. For distributed models the
   coordinator chooses machines and checks connectivity before model startup.
4. Keep using that model for the session so later requests reuse its warm runtime.

OpenCode's [model picker](https://opencode.ai/docs/models/#select-a-model) uses
the provider catalog saved by setup. Listing or selecting a model does not
pre-download it. The advertised list includes models that can be loaded on
demand; it does not mean they are all already loaded or can all run at once.
GPU capacity can change between listing and sending a prompt. PeerFoundry admission
checks still apply, and the client never silently substitutes a smaller model.

### Qwen3 and Devstral on two 12 GB GPUs

For a Qwen3 default with Devstral available for coding, run:

```sh
python3 scripts/setup-opencode.py --model qwen3:14b --title-model qwen3:14b
python3 scripts/run-opencode.py
```

| Model | Placement on this pair | Saved OpenCode context |
| --- | --- | --- |
| `qwen3:14b` | One GPU; default for chat, background work and titles | 8,192 |
| `qwen3:8b` | One GPU; selectable for lighter work | 8,192 |
| `devstral-small-2:24b` | Both GPUs using native llama.cpp RPC | 16,384 |

These saved profiles target this pair and send matching `options.num_ctx` values;
they are not architectural limits. Setup preserves saved per-model overrides.
The generator's portable defaults remain smaller for deployments with less VRAM.
Without a context override, the coordinator selects a bounded context before
placement using the prompt size, model policy and available workers. The saved
OpenCode profiles above send their context explicitly.
Small client windows can cause OpenCode to compact repeatedly before editing;
the saved windows leave more room for its instructions and tool definitions.
The 8K Qwen profiles reserve 1,024 output tokens, leaving about 7K for input.
Reserving half the window for output caused repeated compaction on the coding
fixture. Devstral's 16K profile reserves 4K for output. This follows the pinned
[OpenCode compaction budget calculation](https://github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/session/overflow.ts).
Single-machine Qwen requests default to Ollama's top-level `think: false`, so
short output budgets reach an answer. API callers can explicitly request
`think: true`; returned Ollama thinking is exposed as `reasoning_content`.
Distributed Qwen reasoning behavior is unchanged. See
[Ollama's thinking controls](https://docs.ollama.com/capabilities/thinking).
Qwen3 14B and 8B can process requests on separate machines. Devstral reserves
the pair exclusively; Qwen requests must wait for those resources to be released.
Missing weights download on the agents, and the first distributed load also
transfers worker tensors. The model picker lists loadable models, not residency.

The native Devstral path downloads the text-only Q4_K_M GGUF from
[Unsloth's Devstral Small 2 repository](https://huggingface.co/unsloth/Devstral-Small-2-24B-Instruct-2512-GGUF).
Its revision, size and SHA-256 are pinned in the agent; incomplete or invalid
downloads are discarded before startup. Allow about 14.4 GB of additional disk
space on a machine acting as primary. Ollama's library blob bundles extra vision
tensors that the unmodified native text loader rejects, so that blob is retained
for the single-GPU Ollama path. The distributed route supports text and tools;
it does not load an image projector. See
[Ollama's artifact compatibility layer](https://github.com/ollama/ollama/blob/main/llama/compat/README.md)
for the upstream packaging distinction.
The agent also pins and verifies the model author's updated chat template. The
older embedded template rejects adjacent user messages used by coding clients;
the updated template retains the model's native tool format and accepts them.

Devstral prefers Ollama when a single eligible GPU can hold it with the requested
context. Otherwise the coordinator forms a native cluster, checking the route
and reserving distinct physical hosts. A new machine does not change a live
cluster or take its queued jobs. Ordinary and distributed jobs for the same model
retain separate leases. Busy workers and older queued single-GPU jobs keep their
reservations; a new distributed request receives a capacity rejection if it
cannot obtain enough idle GPUs. No smaller model is substituted.

For a Devstral session, change all OpenCode roles together to avoid background
Qwen requests competing for the same two GPUs:

```sh
python3 scripts/setup-opencode.py --model devstral-small-2:24b --title-model devstral-small-2:24b
python3 scripts/run-opencode.py
```

Use the first command above to restore the Qwen3 14B default. Rerun setup while
the pair is available to refresh the picker, then start a fresh OpenCode session.
Switching `/models` alone leaves background and title routing unchanged. An idle
distributed cluster unloads after the configured RPC idle timeout (900 seconds
on this deployment); an administrator can release it earlier using
`POST /rpc/clusters/{cluster_id}/stop`. This endpoint refuses active requests,
queued cluster work and open streams. Wait for both agents to acknowledge teardown
before requesting a model that needs those GPUs. Capacity rejections carry
`X-Swarm-Status: waiting_for_capacity` and `Retry-After: 5`; the dashboard can wait
and retry without having accepted duplicate work. OpenCode may surface the 503
instead, so retry after capacity is available.

Check the live list without installing, changing config or starting inference:

```powershell
py -3 scripts/setup-opencode.py --base-url https://your-host/peerfoundry/api/v1 --list-models
```

Refresh the saved picker by rerunning normal setup, then restart OpenCode.
The picker is a snapshot; it does not refresh continuously inside an existing
client session when machines join or leave. Saved settings for models that are
no longer advertised are retained but hidden from the picker. The selected
default remains visible even if temporarily unavailable, with a setup warning.

For a sustained switch, update both the main and background model, then restart
OpenCode with a fresh session:

```powershell
py -3 scripts/setup-opencode.py --model qwen3:8b
py -3 scripts/run-opencode.py
```

Selecting another model with `/models` changes the chat model; the configured
`small_model` used for background requests remains the setup selection. Updating
both through setup avoids unexpectedly requesting two different models. Consider
a fresh session after a model switch so old tool/reasoning history does not
consume the new model's context window. Model tool quality and usable context
vary; the live coding checks in this repository cover the 30B A3B profile.

Downloads, cold model loading, prompt processing, and reasoning can make the first
answer slow. Updated distributed RPC primaries now stream reasoning, text and tool
deltas into OpenCode before the final answer. Older agents and the single-machine
Ollama path retain the completed-response fallback. Check the PeerFoundry dashboard for agent
download/startup phases and failures. Avoid repeatedly resubmitting while loading.
Capacity failures may return HTTP 503; switching a large model does not evict an
active cluster. On this deployment, unused RPC clusters unload after 900 seconds.
Wait for capacity to be released before requesting another model that needs the
same GPUs. A context conflict may return HTTP 409 and likewise needs the old
cluster to finish/unload before reforming with a larger window.
See [latency tuning and streaming](performance.md) for repeatable runtime
experiments and bounded streaming/cancellation behavior.

## Optional title routing with a spare machine

When an additional GPU is available, session-title generation can use a separate
model without changing the 30B coding model or summary model:

```powershell
py -3 scripts/setup-opencode.py --title-model qwen3:14b
```

This requires the title model to be advertised by PeerFoundry. Its first request can
load it on an eligible spare agent. Use this only when that spare capacity is
intended for background work. Setup leaves both the main model and `small_model`
on 30B; only `agent.title.model` changes. Existing sessions need a client restart.
To return title work to the main model, set `--title-model qwen3:30b-a3b`.
The option follows the pinned OpenCode implementation's
[title-agent model override](https://github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/session/prompt.ts).

## Credentials

Only a PeerFoundry **consumer key** is needed. Setup and the launcher use this order:

1. `PEERFOUNDRY_CONSUMER_KEY`, then `CONSUMER_KEY`, in the process environment.
2. The checkout's `.env.opencode` (client-only settings).
3. The checkout's `.env` (existing coordinator installations).

Both files accept either variable name. If no key is available, interactive setup
prompts without echo and saves the key to ignored `.env.opencode`; on POSIX the
new file has owner-only permissions. On Windows, access follows the checkout
directory's inherited file permissions. Noninteractive setup requires a key
already supplied through one of those sources. Do not copy coordinator admin or
agent credentials to a client machine.

Supply the URL with `--base-url` or `PEERFOUNDRY_BASE_URL`, or enter it at setup's prompt.
The prompt offers the URL already saved in this checkout as a reusable default.
Noninteractive setup requires one of those sources. The chosen URL is saved as
`PEERFOUNDRY_BASE_URL` in ignored `.env.opencode`; existing private settings are retained.
Setup and the launcher resolve this variable from the environment, then
`.env.opencode`, then `.env`. To change an existing credential, update its
environment variable or private client file.

Generated JSON contains `{env:PEERFOUNDRY_BASE_URL}` and `{env:PEERFOUNDRY_CONSUMER_KEY}`,
keeping both deployment details outside the shared profile. Use the launcher so
it loads these settings into OpenCode's child process. If you run `opencode`
directly, export both `PEERFOUNDRY_BASE_URL` and `PEERFOUNDRY_CONSUMER_KEY` yourself.

Authentication or connection failures stop setup. The check refuses redirects
and accepts HTTPS endpoints, plus HTTP on loopback for local development.
`--offline` explicitly skips discovery; it does not validate credentials or
prove inference works. If a successful catalog check omits the chosen model,
setup reports that fact and retains the selected model. It never silently
switches to a smaller model when GPUs are busy or disconnected.

## Other projects and endpoints

The launcher forwards OpenCode CLI arguments and uses your current working
directory. For example, from this checkout:

```sh
python3 scripts/run-opencode.py /path/to/another/project
```

For a different endpoint or an explicitly smaller context:

```sh
python3 scripts/setup-opencode.py --base-url https://your-host/peerfoundry/api/v1 --context 16384
```

`--model` selects another PeerFoundry API model name without prompting. The generator
provides conservative context/tool profiles for the bounded PeerFoundry catalog and
preserves existing overrides for other models. These are deployment limits,
rather than the weights' architectural maximum context. `--context` tunes the
30B A3B profile; it does not raise the server's limits for other models.
The catalog check verifies authentication and model discovery, not generation.
To exercise actual file reading, editing and test execution in a disposable
workspace, run `python3 scripts/opencode-smoke.py` against the default endpoint.
The smoke runner retains private results under `.rpc-trial/`.
Its `--context` applies to the model being tested. For this checkout's default:

```sh
python3 scripts/opencode-smoke.py --model qwen3:14b --context 8192
```

To check Devstral on the same pair, keep the trial's titles on Devstral too:

```sh
python3 scripts/opencode-smoke.py --model devstral-small-2:24b --context 16384 --title-model devstral-small-2:24b
```

This override affects only the disposable trial, leaving the saved default intact.

## Transport

OpenCode always talks to PeerFoundry through the same HTTPS OpenAI-compatible API.
It needs no QUIC plugin. The coordinator chooses the inter-agent model transport:
direct LAN QUIC first, then eligible relay fallbacks after connectivity checks.
Cloud relay use is currently disabled. See [direct QUIC](direct-quic-inference.md)
and [route selection and recovery](relay-routing.md).

QUIC carries native llama.cpp RPC streams; this is separate from FTP file
transfer. It preserves the existing model, quantization and context settings.
Dated coding checks and environment-specific timings are retained in ignored
`.journal/` files rather than this setup guide.
