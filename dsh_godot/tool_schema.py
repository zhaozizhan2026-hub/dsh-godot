"""Convert MCP tool definitions into OpenAI/DeepSeek function schemas."""

from __future__ import annotations

import fnmatch
from typing import Any, Iterable


def as_dict(value: Any) -> dict[str, Any]:
    """Convert a pydantic MCP object or plain object into a dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "dict") and callable(getattr(value, "dict")):
        dumped = value.dict()
        return dumped if isinstance(dumped, dict) else {}
    return {}


def tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name", "") or "")
    return str(getattr(tool, "name", "") or "")


def tool_description(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("description", "") or "")
    return str(getattr(tool, "description", "") or "")


def tool_input_schema(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        schema = tool.get("inputSchema", None)
    else:
        schema = getattr(tool, "inputSchema", None)
    if schema is None:
        return {"type": "object", "properties": {}}
    return as_dict(schema)


def mcp_tool_to_openai(tool: Any) -> dict[str, Any]:
    """Convert one MCP tool into an OpenAI ``type: function`` tool schema."""
    schema = tool_input_schema(tool)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        # OpenAI chat completions expect a JSON Schema object for parameters.
        # Wrap non-object schemas in an object with a single value property.
        schema = {
            "type": "object",
            "properties": {"value": schema if schema else {}},
            "additionalProperties": True,
        }
    return {
        "type": "function",
        "function": {
            "name": tool_name(tool),
            "description": tool_description(tool),
            "parameters": schema,
        },
    }


def _matches(name: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        pattern = pattern.strip()
        if not pattern:
            continue
        if fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def filter_tools(
    tools: Iterable[Any],
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
) -> list[Any]:
    include = [p for p in (include or []) if p.strip()]
    exclude = [p for p in (exclude or []) if p.strip()]
    out: list[Any] = []
    for tool in tools:
        name = tool_name(tool)
        if not name:
            continue
        if include and not _matches(name, include):
            continue
        if exclude and _matches(name, exclude):
            continue
        out.append(tool)
    return out


def to_openai_tools(
    tools: Iterable[Any],
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    return [mcp_tool_to_openai(tool) for tool in filter_tools(tools, include, exclude)]
