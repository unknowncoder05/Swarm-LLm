#!/usr/bin/env python3
"""Generate the OpenCode provider profile for the Swarm endpoint."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODELS = ("qwen3:30b-a3b", "qwen3:14b", "qwen3:8b", "devstral-small-2:24b", "devstral:24b",
          "ministral-3:8b", "qwen2.5-coder:0.5b", "qwen2.5-coder:1.5b", "qwen2.5-coder:3b",
          "qwen2.5-coder:7b", "qwen2.5-coder:14b", "deepseek-r1:14b")
DEFAULT_BASE_URL = "http://127.0.0.1:8081/swarm/api/v1"
BASE_URL_ENV = "{env:SWARM_BASE_URL}"


def _load_setting(names, root, environ):
    values = os.environ if environ is None else environ
    for name in names:
        if values.get(name):
            return values[name]
    for path in (root / ".env.opencode", root / ".env"):
        entries = {}
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                if name.strip() not in names:
                    continue
                value = value.strip()
                if value.startswith('"') and value.endswith('"'):
                    value = json.loads(value)
                else:
                    value = value.strip("'")
                entries[name.strip()] = value
        for name in names:
            if entries.get(name):
                return entries[name]
    return ""


def load_consumer_key(root=ROOT, environ=None):
    """Environment overrides client-only credentials, then coordinator .env."""
    return _load_setting(("SWARM_CONSUMER_KEY", "CONSUMER_KEY"), root, environ)


def load_base_url(root=ROOT, environ=None, default=DEFAULT_BASE_URL):
    return _load_setting(("SWARM_BASE_URL",), root, environ) or default


def save_client_settings(root, base_url, key=None):
    path = root / ".env.opencode"
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    settings = {"SWARM_BASE_URL": base_url}
    if key is not None:
        settings["SWARM_CONSUMER_KEY"] = key
    lines = [line for line in lines if line.split("=", 1)[0].strip() not in settings]
    write_private(path, "\n".join(lines + [name + "=" + json.dumps(value) for name, value in settings.items()]) + "\n")


def merge_config(existing, generated):
    """Update owned settings while retaining other providers, tools and policies."""
    if not isinstance(existing, dict):
        raise ValueError("OpenCode config must be a JSON object")
    result = dict(existing)
    for key, value in generated.items():
        if isinstance(value, dict) and key in result:
            if not isinstance(result[key], dict):
                raise ValueError("OpenCode config field %s must be an object" % key)
            result[key] = merge_config(result[key], value)
        else:
            result[key] = value
    # Existing provider filters must not hide the provider being configured.
    if "provider" in generated and "swarm-llm" in generated["provider"]:
        if "enabled_providers" in result:
            if not isinstance(result["enabled_providers"], list):
                raise ValueError("enabled_providers must be an array")
            result["enabled_providers"] = list(dict.fromkeys([*result["enabled_providers"], "swarm-llm"]))
        if "disabled_providers" in result:
            if not isinstance(result["disabled_providers"], list):
                raise ValueError("disabled_providers must be an array")
            result["disabled_providers"] = [p for p in result["disabled_providers"] if p != "swarm-llm"]
    return result


def write_private(path, content):
    """Replace a file atomically using owner-only permissions on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update_config(path, config):
    existing = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    write_private(path, json.dumps(merge_config(existing, config), indent=2) + "\n")


def choose_model(advertised, requested=None):
    if requested:
        return requested
    if "qwen3:30b-a3b" in advertised:
        return "qwen3:30b-a3b"
    return next(iter(advertised), "qwen2.5-coder:1.5b")


def build_config(model, base_url=BASE_URL_ENV, context=32768, advertised=None):
    if type(context) is not int or not 512 <= context <= 32768:
        raise ValueError("context must be an integer between 512 and 32768")
    names = MODELS if advertised is None else advertised
    models = {name: {"name": name} for name in dict.fromkeys((*names, model))}
    # Other deployments may expose custom models; retain their existing metadata.
    # These bounded profiles match Swarm's conservative minimum context policies.
    profiles = {"qwen3:8b": 2048, "qwen3:14b": 4096, "gemma4:12b": 2048,
                "devstral-small-2:24b": 2048, "qwen3.6:27b": 4096,
                "qwen3:30b": 4096, "qwen3:32b": 4096, "qwen3:30b-a3b": context}
    for name, window in profiles.items():
        if name not in models:
            continue
        profile = models[name]
        profile.update(tool_call=True, limit={"context": window, "output": min(4096, window // 2)})
        if name.startswith("qwen3") or name.startswith("gemma4"):
            profile.update(reasoning=True, interleaved={"field": "reasoning_content"})
        if name == "qwen3:30b-a3b":
            profile["options"] = {"options": {"num_ctx": context}}
    config = {"$schema": "https://opencode.ai/config.json", "model": "swarm-llm/" + model,
        "small_model": "swarm-llm/" + model,
        "provider": {"swarm-llm": {"npm": "@ai-sdk/openai-compatible", "name": "Swarm LLM",
            "options": {"baseURL": base_url.rstrip("/"), "apiKey": "{env:SWARM_CONSUMER_KEY}",
                        "timeout": 3700000, "headerTimeout": 3700000, "chunkTimeout": 3700000},
            "models": models}}}
    if advertised is not None:
        # Keep saved overrides, but hide stale static entries from the picker.
        config["provider"]["swarm-llm"]["whitelist"] = list(models)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Pin a model instead of selecting from the advertised catalog")
    parser.add_argument("--base-url", default=load_base_url())
    parser.add_argument("--context", type=int, default=32768, choices=range(512, 32769), metavar="512..32768")
    parser.add_argument("--output", type=Path, default=ROOT / "opencode.json")
    args = parser.parse_args()
    key = load_consumer_key()
    if not key:
        parser.error("CONSUMER_KEY must be supplied in the environment or project .env")
    advertised = []
    try:
        request = urllib.request.Request(args.base_url.rstrip("/") + "/models", headers={"X-API-Key": key})
        with urllib.request.urlopen(request, timeout=5) as response:
            advertised = [model["id"] for model in json.load(response).get("data", [])]
    except Exception:
        pass
    model = choose_model(advertised, args.model)
    config = build_config(model, BASE_URL_ENV, args.context)
    save_client_settings(ROOT, args.base_url)
    update_config(args.output, config)
    print(f"{args.output}: swarm-llm/{model} ({'advertised' if model in advertised else 'not currently advertised'})")
    print("Use python3 scripts/run-opencode.py to load the consumer credential privately from .env.")


if __name__ == "__main__":
    main()
