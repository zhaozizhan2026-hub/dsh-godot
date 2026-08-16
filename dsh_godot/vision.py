"""Optional image -> text description for the Godot dock frontend.

DeepSeek V4 is text-only.  The dock can still capture and display screenshots;
this module optionally sends the PNG to any OpenAI-compatible vision model
(Groq, OpenAI, xAI, ...) and returns a compact text description that gets
appended to the conversation as a user message.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from .config import BridgeConfig


async def describe_png(png_bytes: bytes, config: BridgeConfig) -> str | None:
    if not config.vision_enabled or not png_bytes:
        return None
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    payload: dict[str, Any] = {
        "model": config.vision_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": config.vision_prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "max_tokens": config.vision_max_tokens,
    }
    endpoint = config.vision_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + config.vision_api_key,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(endpoint, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        return str(data["choices"][0]["message"]["content"]).strip()
