# Qwen3.8-27B across GPU machines

The upstream [Qwen/Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B) is a dense
27B model built on the Qwen3.5 hybrid architecture. PeerFoundry uses the explicit
[Ollama Q4_K_M artifact](https://ollama.com/library/qwen3.8:27b-q4_K_M),
`qwen3.8:27b-q4_K_M`, for its existing llama.cpp RPC path.

The API also accepts `Qwen/Qwen3.8-27B` and `qwen3.8:27b` as aliases for that
artifact. Discovery advertises the explicit quantized name; responses identify
the actual runtime model. This deployment uses quantized GGUF weights rather than
the upstream BF16 Transformers weights.

The catalog reserves at least 19 GB combined reported VRAM and defaults to a
32,768-token context. Two 12 GB GPUs are eligible; one 12 GB GPU is not. The
primary downloads the Ollama package when absent, resolves its model GGUF, and
distributes layers to its reserved worker through the verified QUIC route.
Native fit, complete layer offload and actual generation must still be checked;
VRAM estimates alone do not establish readiness.

The first load also fills the remote RPC tensor cache. A 100% Ollama download
does not mean the replica is ready: native startup can still be transferring
several GB to the second machine. Use the replica state, QUIC byte counters and
native logs to distinguish an advancing cold load from a failure. The worker's
cache can avoid sending unchanged tensors again on later loads.

A live trial on two RTX 4070 SUPER 12 GB workers with native runtime `b11612`
confirmed a 32K slot and 66/66 GPU layers. Public API checks passed streamed
arithmetic, a structured tool call, an English greeting and reference-label
recall across a 3,407-token prompt.

Reasoning remains enabled by the model's native template. The agent uses the
Ollama model's sampling defaults (temperature 1.0, top-p 0.95, top-k 20, min-p 0,
presence penalty 0 and repeat penalty 1) while preserving explicit overrides.
The 30B-A3B model retains its separate sampling profile.

The trial uses batch 128, microbatch 64 and disables the RAM prompt-snapshot cache
for this model. Merge this entry into the existing private runtime profile map
rather than replacing other model settings:

```dotenv
RPC_MODEL_RUNTIME_PROFILES={"qwen3.8:27b-q4_K_M":{"cache_ram":0}}
```

This changes snapshot retention, not model weights, reasoning or KV precision.

To configure OpenCode after the model is advertised:

```sh
python3 scripts/setup-opencode.py --model qwen3.8:27b-q4_K_M
python3 scripts/run-opencode.py
```

For an actual streamed inference check, allowing time for the first download/load:

```sh
python3 scripts/rpc-stream-smoke.py --model qwen3.8:27b-q4_K_M --timeout 1800
```

Both ordinary and background requests use the selected model unless another
background override was explicitly configured. The client profile advertises
reasoning, tool calls and the same 32K context used for native startup.

Changing from an active 30B cluster requires an idle teardown first. The
coordinator never evicts busy work to make room. After startup, use the dashboard
to confirm both GPUs belong to the new replica and report full native layer
offload, then check actual responses and tool use.

A dense 27B model can require more computation per generated token than the
30B model with only 3B active parameters. Fitting on the same machines does not
promise lower latency. The current native path handles text and tools; although
the download includes a vision projector, this integration does not pass it to
llama-server and does not claim image/video support.
