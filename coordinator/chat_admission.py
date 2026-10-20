"""Validate and normalize consumer input before reserving inference capacity."""

import json
from typing import Any

try:
    from validation import validate_contract
except ModuleNotFoundError:
    from coordinator.validation import validate_contract


class InvalidChatRequest(ValueError):
    """A consumer request cannot be forwarded safely to an inference worker."""


def _output_budget(body: dict, options: dict) -> int:
    limits = []
    for field in ("max_tokens", "max_completion_tokens"):
        if field in body:
            value = body[field]
            limits.append(value)
    native = options.get("num_predict", -1)
    if native != -1:
        limits.append(native)
    # Honor every supplied cap; omitted caps retain the previous unlimited default.
    return min(limits, default=-1)


def prepare_chat_request(body: Any) -> dict:
    """Normalize an owned JSON body without network calls or scheduler mutations."""
    try:
        validate_contract("chat-request", body)
    except ValueError as exc:
        raise InvalidChatRequest(str(exc)) from None
    options = body.setdefault("options", {})
    messages = body["messages"]
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call["function"]
            if isinstance(function.get("arguments"), str):
                try:
                    arguments = json.loads(function["arguments"])
                except json.JSONDecodeError:
                    continue  # Preserve incomplete tool arguments for the backend.
                if isinstance(arguments, dict):
                    function["arguments"] = arguments
    options["num_predict"] = _output_budget(body, options)
    if not any(message["role"] in ("system", "developer") for message in messages):
        body["messages"] = [
            {
                "role": "system",
                "content": "You are a helpful assistant. Reply in the language of the user's latest message unless they "
                "explicitly request another language. Reply to English messages in English.",
            },
            *messages,
        ]
    return body
