# Agent snapshot

`agent.py` preserves the reference agent used by the disposable GPU lab. The
snapshot has deployment-specific comments removed; its executable behavior is
separate from the current agent in `agents/`.

SHA-256 of this snapshot:

```text
e93a4dfc214872c275ca55acd7a8b76a53b684e96b2e662a6e104d0e92c9a305
```

The lab uploads the snapshot, applies its adapters at process startup, and
disables self-updates. To test another reviewed source, set `agent_source_path`
to its local path. Terraform records the content hash and replaces the instances.
Keep the required automatic RPC methods compatible with `scripts/launch.py`.
