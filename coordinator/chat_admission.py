"""Validate and normalize consumer input before reserving inference capacity."""
import json
from typing import Any


class InvalidChatRequest(ValueError):
    """A consumer request cannot be forwarded safely to an inference worker."""


def _output_budget(body: dict, options: dict) -> int:
    limits = []
    for field in ("max_tokens", "max_completion_tokens"):
        if field in body:
            value = body[field]
            if type(value) is not int or not 1 <= value <= 65536:
                raise InvalidChatRequest(f"{field} must be an integer between 1 and 65536")
            limits.append(value)
    native = options.get("num_predict", -1)
    if type(native) is not int or not (native == -1 or 1 <= native <= 65536):
        raise InvalidChatRequest("options.num_predict must be -1 or an integer between 1 and 65536")
    if native != -1:
        limits.append(native)
    # Honor every supplied cap; omitted caps retain the previous unlimited default.
    return min(limits, default=-1)


def prepare_chat_request(body: Any) -> dict:
    """Normalize an owned JSON body without network calls or scheduler mutations."""
    if not isinstance(body, dict):
        raise InvalidChatRequest("Request body must be an object")
    if not isinstance(body.get("model"), str) or not body["model"].strip():
        raise InvalidChatRequest("model must be a nonempty string")
    options = body.setdefault("options", {})
    if not isinstance(options, dict):
        raise InvalidChatRequest("options must be an object")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise InvalidChatRequest("messages must be a nonempty array")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in (
                "system", "developer", "user", "assistant", "tool", "function"):
            raise InvalidChatRequest("Each message must be an object with a supported role")
        content = message.get("content")
        if content is not None and not isinstance(content, (str, list)):
            raise InvalidChatRequest("Message content must be a string, an array, or null")
        if isinstance(content, list) and any(not isinstance(part, dict) for part in content):
            raise InvalidChatRequest("Content array entries must be objects")
        calls = message.get("tool_calls")
        if calls is not None and not isinstance(calls, list):
            raise InvalidChatRequest("tool_calls must be an array")
        for call in calls or []:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise InvalidChatRequest("Each tool call must contain a function object")
            function = call["function"]
            if isinstance(function.get("arguments"), str):
                try:
                    arguments = json.loads(function["arguments"])
                except json.JSONDecodeError:
                    continue  # Preserve incomplete tool arguments for the backend.
                if isinstance(arguments, dict):
                    function["arguments"] = arguments
    if "stream" in body and type(body["stream"]) is not bool:
        raise InvalidChatRequest("stream must be a boolean")
    if "stream_options" in body and not isinstance(body["stream_options"], dict):
        raise InvalidChatRequest("stream_options must be an object")
    tools = body.get("tools")
    if tools is not None and (not isinstance(tools, list) or any(
            not isinstance(tool, dict) or not isinstance(tool.get("function"), dict) for tool in tools)):
        raise InvalidChatRequest("tools must be an array of function definitions")
    options["num_predict"] = _output_budget(body, options)
    if not any(message["role"] in ("system", "developer") for message in messages):
        body["messages"] = [{"role": "system", "content":
            "You are a helpful assistant. Reply in the language of the user's latest message unless they "
            "explicitly request another language. Reply to English messages in English."}, *messages]
    return body
