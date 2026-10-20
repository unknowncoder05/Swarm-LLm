# Distributed inference performance

Keep model weights, quantization, reasoning, context and KV-cache precision fixed
when comparing performance. GPU compute utilization is not spare decode capacity:
layer splitting executes parts of each token sequentially across machines, with
network synchronization and memory access between them. More machines can add
capacity without improving an individual response.

## Model-specific runtime settings

`RPC_MODEL_RUNTIME_PROFILES` maps model names to validated defaults for future
clusters. The selected 30B setting retains its original batch sizes and disables
RAM prompt snapshots:

```dotenv
RPC_MODEL_RUNTIME_PROFILES='{"qwen3:30b-a3b":{"batch_size":128,"ubatch_size":64,"cache_ram":0}}'
```

Other models retain native defaults. Settings do not retune an already-running
process; finish its work and let it stop before testing another profile. Removing
the entry restores native defaults for future clusters. Overrides accept only
bounded batch size, microbatch size and snapshot capacity, never arbitrary flags.
The selected primary must advertise tuning capability.

Disabling RAM snapshots preserves ordinary running-slot prefix reuse. It removes
snapshot restoration overhead, but switching back to an older conversation may
require processing its prompt again. Larger batches can improve initial prompt
processing while making the complete request sequence slower. Measure both
continuations and conversation switching. See the pinned runtime's
[cache and batch options](https://github.com/ggml-org/llama.cpp/blob/b11612/tools/server/README.md).

## Repeatable checks

```sh
# Preview, then run against exactly two named online agents:
python3 scripts/rpc-benchmark.py --hosts PRIMARY_HOST WORKER_HOST
python3 scripts/rpc-benchmark.py --hosts PRIMARY_HOST WORKER_HOST --repeats 2 --apply
python3 scripts/rpc-stream-smoke.py
python3 scripts/opencode-smoke.py --timeout 900
```

The benchmark pins agent identities and checks their current source/capabilities,
correct answers, requested context and full GPU layer offload. Startup is reported
separately from short, long, follow-up and restored-prompt cases. Between profiles,
the admin-only idle-stop endpoint rejects active requests, queued work and streams
still draining. Physical reservations remain until native teardown is acknowledged.
Trials wait for user work to finish and never substitute another machine.

Generation token counts can differ across cache states and batch sizes. A timing
change on one fixture is not a general quality or GPU-throughput result. The
scripts save private evidence under `.rpc-trial/`; dated run journals live in
the ignored `.journal/` directory.

## Streaming and OpenCode

Updated RPC agents publish native reasoning, text and indexed tool-call deltas
through bounded authenticated event uploads. The coordinator forwards SSE before
completion and commits finish/usage with the accepted final result. Identical
retries are idempotent; stale claims and reordered batches fail. Backpressure and
disconnect cancellation retain GPU ownership until completion or teardown.
Partially emitted responses cannot be replayed automatically. Older agents and
single-machine Ollama requests retain completed-response streaming.

The public stream smoke measures the first meaningful delta, excluding role-only
events and keepalive comments. Streaming reduces time to visible progress; it
does not increase native token generation speed. Large coding prompts and long
reasoning turns can still take minutes.

With spare single-machine capacity, `scripts/setup-opencode.py --title-model
qwen3:14b` moves only title generation off the 30B coding model. Main and summary
models retain the selected 30B profile and 32K context. The title model must be
advertised by the live catalog. See [OpenCode setup](opencode-setup.md),
[architecture](architecture.md) and [startup waiting](ui-startup-wait.md).
