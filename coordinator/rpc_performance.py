"""Bounded native settings for reproducible RPC performance experiments."""

import json

try:
    from validation import validate_contract
except ModuleNotFoundError:
    from coordinator.validation import validate_contract
DEFAULT_RUNTIME = {"batch_size": 128, "ubatch_size": 64, "cache_ram": 8192}


def validate_runtime(value):
    validate_contract("rpc-runtime", value)
    runtime = dict(DEFAULT_RUNTIME, **value)
    if runtime["ubatch_size"] > runtime["batch_size"]:
        raise ValueError("ubatch_size cannot exceed batch_size")
    return runtime


def model_runtime_profiles(raw):
    values = json.loads(raw or "{}")
    validate_contract("runtime-profiles", values)
    return {name: validate_runtime(profile) for name, profile in values.items()}
