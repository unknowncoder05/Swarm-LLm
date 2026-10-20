"""Bounded native settings for reproducible RPC performance experiments."""
import json
DEFAULT_RUNTIME = {"batch_size": 128, "ubatch_size": 64, "cache_ram": 8192}


def validate_runtime(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_RUNTIME):
        raise ValueError("rpc_runtime accepts batch_size, ubatch_size and cache_ram only")
    runtime = dict(DEFAULT_RUNTIME, **value)
    for name, choices in (("batch_size", (128, 256, 512)), ("ubatch_size", (64, 128, 256)),
                          ("cache_ram", (0, 1024, 4096, 8192))):
        if type(runtime[name]) is not int or runtime[name] not in choices:
            raise ValueError("Invalid RPC runtime setting: " + name)
    if runtime["ubatch_size"] > runtime["batch_size"]:
        raise ValueError("ubatch_size cannot exceed batch_size")
    return runtime


def model_runtime_profiles(raw):
    values = json.loads(raw or "{}")
    if (not isinstance(values, dict) or len(values) > 16
            or any(not isinstance(name, str) or not 1 <= len(name) <= 200 for name in values)):
        raise ValueError("RPC_MODEL_RUNTIME_PROFILES must map at most 16 model names to settings")
    return {name: validate_runtime(profile) for name, profile in values.items()}
