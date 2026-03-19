from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from opencompany.config import McpServerConfig

MCP_DYNAMIC_TOOL_PREFIX = "mcp__"
MCP_MAX_INLINE_TEXT_CHARS = 20_000
MCP_MAX_INLINE_BINARY_BYTES = 16_384
MCP_MAX_LIST_ITEMS = 500


def sanitize_identifier(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        return fallback
    if normalized[0].isdigit():
        return f"n_{normalized}"
    return normalized


def synthetic_tool_name(server_id: str, tool_name: str) -> str:
    normalized_server = sanitize_identifier(server_id, fallback="server")
    normalized_tool = sanitize_identifier(tool_name, fallback="tool")
    base = f"{MCP_DYNAMIC_TOOL_PREFIX}{normalized_server}__{normalized_tool}"
    digest = hashlib.sha256(f"{server_id}::{tool_name}".encode("utf-8")).hexdigest()[:8]
    return f"{base}__{digest}"


def expand_header_value(value: str) -> str:
    normalized = str(value or "")
    if normalized.startswith("env:"):
        return os.environ.get(normalized[4:].strip(), "")
    return normalized


def is_local_http_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    host = str(parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def should_expose_roots(
    *,
    server: McpServerConfig,
    workspace_is_remote: bool,
) -> bool:
    if workspace_is_remote:
        return False
    if server.expose_roots is not None:
        return bool(server.expose_roots)
    if server.transport == "stdio":
        return True
    return is_local_http_url(server.url)


def filter_allowed_tools(
    tool_names: list[str],
    allowed_tools: list[str],
) -> list[str]:
    normalized_allowed = {
        str(item).strip()
        for item in allowed_tools
        if str(item).strip()
    }
    if not normalized_allowed:
        return list(tool_names)
    return [name for name in tool_names if name in normalized_allowed]


def truncate_text_payload(text: str) -> tuple[str, bool]:
    normalized = str(text or "")
    if len(normalized) <= MCP_MAX_INLINE_TEXT_CHARS:
        return normalized, False
    return normalized[:MCP_MAX_INLINE_TEXT_CHARS], True


@dataclass(slots=True)
class McpToolDescriptor:
    server_id: str
    server_title: str
    tool_name: str
    synthetic_name: str
    description: str = ""
    title: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    def to_tool_definition(self) -> dict[str, Any]:
        parameters = (
            dict(self.input_schema)
            if isinstance(self.input_schema, dict)
            else {"type": "object", "properties": {}, "additionalProperties": True}
        )
        if parameters.get("type") != "object":
            parameters = {"type": "object", "properties": {}, "additionalProperties": True}
        description_parts = [
            part
            for part in [
                self.description.strip(),
                f"MCP server: {self.server_title} ({self.server_id}); original tool: {self.tool_name}.",
            ]
            if part
        ]
        return {
            "type": "function",
            "function": {
                "name": self.synthetic_name,
                "description": " ".join(description_parts).strip(),
                "parameters": parameters,
            },
        }


@dataclass(slots=True)
class McpResourceDescriptor:
    server_id: str
    server_title: str
    uri: str
    name: str = ""
    title: str = ""
    description: str = ""
    mime_type: str = ""


@dataclass(slots=True)
class McpServerRuntimeState:
    server_id: str
    title: str
    transport: str
    enabled: bool
    connected: bool = False
    roots_enabled: bool = False
    protocol_version: str = ""
    warning: str = ""
    tool_count: int = 0
    resource_count: int = 0
    tools_dirty: bool = False
    resources_dirty: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.server_id,
            "title": self.title,
            "transport": self.transport,
            "enabled": self.enabled,
            "connected": self.connected,
            "roots_enabled": self.roots_enabled,
            "protocol_version": self.protocol_version,
            "warning": self.warning,
            "tool_count": self.tool_count,
            "resource_count": self.resource_count,
            "tools_dirty": self.tools_dirty,
            "resources_dirty": self.resources_dirty,
        }
