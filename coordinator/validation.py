"""Load reviewed JSON Schema contracts once; report failures without payloads."""

from functools import lru_cache
import json
from pathlib import Path

from jsonschema import Draft202012Validator, validators

# JSON Schema permits 1.0 as an integer. Our wire contracts retain the existing
# strict integer policy for token budgets, native switches and array indexes.
StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, value: type(value) is int
    ),
)
SCHEMA_DIR = Path(__file__).with_name("schemas")


@lru_cache(maxsize=16)
def contract(name):
    schema = json.loads((SCHEMA_DIR / (name + ".json")).read_text(encoding="utf-8"))
    StrictValidator.check_schema(schema)
    return StrictValidator(schema)


def validate_contract(name, value):
    """Raise ValueError containing the contract location, never submitted data."""
    error = next(contract(name).iter_errors(value), None)
    if error is not None:
        location = "/" + "/".join(str(part) for part in error.absolute_path)
        raise ValueError(f"Invalid {name} at {location}: {error.validator} constraint")
