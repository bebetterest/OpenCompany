from __future__ import annotations

import json
import re
from typing import Any

from opencompany.models import ToolCall


class ProtocolError(ValueError):
    pass


JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any]:
    match = JSON_BLOCK_RE.search(text)
    candidate = match.group(1) if match else text.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ProtocolError("No JSON object found in model response")
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"Invalid JSON response: {exc}") from exc


def normalize_actions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ProtocolError("Response must contain a non-empty actions list")
    normalized: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict) or "type" not in action:
            raise ProtocolError("Each action must be an object with a type")
        normalized.append(action)
    return normalized


def normalize_tool_calls(tool_calls: list[ToolCall]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if not tool_call.name:
            raise ProtocolError("Tool call is missing a function name")
        arguments_text = tool_call.arguments_json.strip() or "{}"
        try:
            arguments = json.loads(arguments_text)
        except json.JSONDecodeError as exc:
            raise ProtocolError(
                f"Invalid tool arguments for '{tool_call.name}': {exc}"
            ) from exc
        if not isinstance(arguments, dict):
            raise ProtocolError(
                f"Tool '{tool_call.name}' arguments must decode to a JSON object"
            )
        normalized.append(
            {
                "type": tool_call.name,
                "_tool_call_id": tool_call.id,
                **arguments,
            }
        )
    return normalized
