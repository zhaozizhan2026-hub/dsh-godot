"""Serialize MCP tool results into text suitable for DeepSeek's text-only API."""

from __future__ import annotations

import json
from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
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
    return {"text": str(value)}


def _compact_json(value: Any, max_chars: int = 20_000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return truncate_text(text, max_chars)


def _image_block_text(block: dict[str, Any]) -> str:
    data = block.get("data", "") or ""
    mime = block.get("mimeType", "") or "unknown"
    size = len(data) if isinstance(data, str) else 0
    # DeepSeek V4 models used through this bridge are text-only.  Keep the
    # image block informative but do not ship megabytes of base64 into the
    # prompt.  If Godot AI's Vision Routing produced a description, it
    # arrives as a separate text content block and is preserved below.
    return "[image content omitted for text-only model: mime=%s, base64_chars=%d]" % (
        mime,
        size,
    )


def _audio_block_text(block: dict[str, Any]) -> str:
    data = block.get("data", "") or ""
    mime = block.get("mimeType", "") or "unknown"
    return "[audio content omitted for text-only model: mime=%s, base64_chars=%d]" % (
        mime,
        len(data) if isinstance(data, str) else 0,
    )


def _resource_block_text(block: dict[str, Any]) -> str:
    resource = as_dict(block.get("resource"))
    if resource.get("text") is not None:
        return truncate_text(str(resource["text"]), 50_000)
    uri = resource.get("uri", "") or ""
    blob = resource.get("blob", "") or ""
    if uri:
        return "[resource uri=%s]" % uri
    return "[embedded resource, base64_chars=%d]" % (
        len(blob) if isinstance(blob, str) else 0
    )


def content_block_to_text(block: Any) -> str:
    item = as_dict(block)
    kind = str(item.get("type", "text") or "text")
    if kind == "text":
        text = item.get("text")
        return "" if text is None else str(text)
    if kind == "image":
        return _image_block_text(item)
    if kind == "audio":
        return _audio_block_text(item)
    if kind == "resource":
        return _resource_block_text(item)
    if kind == "resource_link":
        return "[resource_link uri=%s]" % (item.get("uri", "") or "")
    return _compact_json(item, 20_000)


def truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max(0, max_chars - 120)
    omitted = len(text) - keep
    return text[:keep] + (
        "\n\n[... tool result truncated: %d additional characters omitted ...]" % omitted
    )


def serialize_call_tool_result(result: Any, max_chars: int = 100_000) -> str:
    """Convert an MCP ``CallToolResult`` (or dict/fake) into prompt text."""
    item = as_dict(result)
    is_error = bool(item.get("isError", False))

    content = item.get("content")
    if content:
        blocks: list[str] = []
        for block in content:
            text = content_block_to_text(block)
            if text:
                blocks.append(text)
        payload = "\n\n".join(blocks) if blocks else "(tool returned empty content)"
    elif item.get("structuredContent") is not None:
        payload = _compact_json(item["structuredContent"], max_chars)
    else:
        payload = "(tool returned no content)"

    if is_error:
        payload = "TOOL ERROR:\n" + payload
    return truncate_text(payload, max_chars)


def serialize_tool_exception(exc: BaseException, max_chars: int = 100_000) -> str:
    text = "TOOL CALL FAILED:\n%s: %s" % (type(exc).__name__, exc)
    return truncate_text(text, max_chars)
