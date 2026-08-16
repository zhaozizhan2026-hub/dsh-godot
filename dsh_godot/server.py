"""WebSocket service consumed by the Godot dsh dock.

Protocol (JSON):

    client -> server:
      {"type": "chat", "prompt": "..."}
      {"type": "clear"}
      {"type": "screenshot", "image_base64": "...", "mime": "image/png"}
      {"type": "ping"}

    server -> client:
      {"type": "status", "message": "...", "level": "ok|warn|error"}
      {"type": "user", "text": "..."}
      {"type": "assistant", "text": "..."}
      {"type": "reasoning", "text": "..."}
      {"type": "tool_call", "text": "..."}
      {"type": "tool_result", "text": "..."}
      {"type": "image", "path": "absolute/path.png", "tool": "..."}
      {"type": "files_changed", "paths": [...]}
      {"type": "vision", "text": "..."}
      {"type": "done", "text": "...", "tool_calls": 3}
      {"type": "error", "message": "..."}
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import websockets

from .agent import AgentCancelled, DeepSeekGodotAgent, TurnTimeout
from .config import BridgeConfig
from .tools import CompositeToolProvider
from .vision import describe_png


class DshGodotService:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.harness: Any = None
        self.provider = CompositeToolProvider(config, asyncio.Queue())
        self.agent: DeepSeekGodotAgent | None = None
        self.history: list[dict[str, Any]] = []
        self.clients: set[Any] = set()
        self._chat_lock = asyncio.Lock()
        self._stop_event: asyncio.Event | None = None
        self._broadcast_task: asyncio.Task | None = None

    def _build_harness(self) -> None:
        if not self.config.api_key:
            return
        from deepseek_harness import DeepSeekHarness

        self.harness = DeepSeekHarness(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            salvage_tool_calls=self.config.salvage_tool_calls,
            normalize_cache_fields=self.config.normalize_cache_fields,
            warn_on_missing_reasoning=self.config.warn_on_missing_reasoning,
            disable_thinking_by_default=not self.config.thinking,
            raw_dump_path=self.config.raw_dump_path or None,
        )

    async def start(self) -> None:
        self._build_harness()
        if self.harness is not None:
            self._stop_event = asyncio.Event()
            self.agent = DeepSeekGodotAgent(
                self.config,
                self.harness,
                self.provider,
                output=self._agent_output,
                cancel_event=self._stop_event,
            )
        self.config.screenshot_dir_path().mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        if self._broadcast_task is not None:
            self._broadcast_task.cancel()
            self._broadcast_task = None
        await self.provider.close()

    async def run(self) -> None:
        await self.start()
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        print(
            "dsh-godot service listening on ws://%s:%d"
            % (self.config.ws_host, self.config.dock_port),
            flush=True,
        )
        try:
            async with websockets.serve(
                self._handle, self.config.ws_host, self.config.dock_port
            ):
                # Bind the WebSocket first, then probe Godot AI MCP in the
                # background.  A missing/hanging MCP endpoint must never delay
                # the dock connection or make the service look "not started".
                provider_task = asyncio.create_task(self.provider.start())
                await provider_task
                await asyncio.Future()
        finally:
            await self.close()

    # ------------------------------------------------------------------
    async def _handle(self, websocket) -> None:
        self.clients.add(websocket)
        try:
            await self._send(
                websocket,
                {
                    "type": "status",
                    "message": "dsh-godot connected",
                    "level": "ok",
                },
            )
            if self.harness is None:
                await self._send(
                    websocket,
                    {
                        "type": "error",
                        "message": (
                            "DEEPSEEK_API_KEY is missing. Put it in the project "
                            ".env file and restart the service."
                        ),
                    },
                )
            await self._send(
                websocket,
                {
                    "type": "status",
                    "message": "Godot AI MCP: %s"
                    % (
                        "online"
                        if self.provider.godot_online
                        else "offline - " + self.provider.godot_error
                    ),
                    "level": "ok" if self.provider.godot_online else "warn",
                },
            )
            await self._send(
                websocket,
                {
                    "type": "mode",
                    "thinking": self.config.thinking,
                    "web": self.config.web_enabled,
                    "max_tokens": self.config.max_tokens,
                    "stream": self.config.stream,
                    "max_tool_turns": self.config.max_tool_turns,
                    "parallel_tools": self.config.parallel_tools,
                    "model": self.config.model,
                },
            )
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send(
                        websocket, {"type": "error", "message": "invalid JSON"}
                    )
                    continue
                await self._dispatch(websocket, message)
        finally:
            self.clients.discard(websocket)

    async def _dispatch(self, websocket, message: dict[str, Any]) -> None:
        kind = str(message.get("type", ""))
        if kind == "ping":
            await self._send(websocket, {"type": "pong"})
        elif kind == "clear":
            self.history.clear()
            await self._broadcast(
                {"type": "status", "message": "conversation cleared", "level": "ok"}
            )
        elif kind == "chat":
            prompt = str(message.get("prompt", "") or "").strip()
            if not prompt:
                await self._send(
                    websocket, {"type": "error", "message": "empty prompt"}
                )
                return
            await self._chat(websocket, prompt)
        elif kind == "mode":
            await self._set_mode(message)
        elif kind == "stop":
            await self._stop_chat()
        elif kind == "screenshot":
            await self._screenshot(websocket, message)
        else:
            await self._send(
                websocket, {"type": "error", "message": "unknown type: %s" % kind}
            )

    async def _stop_chat(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        await self._broadcast(
            {"type": "status", "message": "停止请求已发送", "level": "warn"}
        )

    async def _set_mode(self, message: dict[str, Any]) -> None:
        """Runtime mode switch: model / thinking / web / limits."""
        if message.get("model"):
            self.config.model = str(message.get("model")).strip()
        if "thinking" in message:
            self.config.thinking = bool(message.get("thinking"))
        if "web" in message:
            self.config.web_enabled = bool(message.get("web"))
        if "max_tokens" in message:
            max_tokens = int(message.get("max_tokens"))
            if max_tokens >= 0:
                self.config.max_tokens = max_tokens
        if "stream" in message:
            self.config.stream = bool(message.get("stream"))
        if "max_tool_turns" in message:
            max_tool_turns = int(message.get("max_tool_turns"))
            if max_tool_turns >= 0:
                self.config.max_tool_turns = max_tool_turns
        if "parallel_tools" in message:
            self.config.parallel_tools = bool(message.get("parallel_tools"))
        await self._broadcast(
            {
                "type": "mode",
                "thinking": self.config.thinking,
                "web": self.config.web_enabled,
                "max_tokens": self.config.max_tokens,
                "stream": self.config.stream,
                "max_tool_turns": self.config.max_tool_turns,
                "parallel_tools": self.config.parallel_tools,
                "model": self.config.model,
            }
        )

    async def _chat(self, websocket, prompt: str) -> None:
        if self.agent is None:
            await self._send(
                websocket,
                {
                    "type": "error",
                    "message": "DeepSeek harness is not configured; set DEEPSEEK_API_KEY.",
                },
            )
            return
        if self._chat_lock.locked():
            await self._send(
                websocket,
                {
                    "type": "error",
                    "message": "an agent turn is already running; wait for 'done'.",
                },
            )
            return
        await self._broadcast(
            {"type": "user", "text": prompt}
        )
        async with self._chat_lock:
            if self._stop_event is not None:
                self._stop_event.clear()
            self.history = DeepSeekGodotAgent.prepare_for_new_user_turn(self.history)
            try:
                result = await self.agent.run(prompt, history=self.history)
            except AgentCancelled as exc:
                await self._broadcast(
                    {
                        "type": "stopped",
                        "message": "conversation stopped by user",
                    }
                )
                return
            except TurnTimeout as exc:
                await self._broadcast(
                    {
                        "type": "turn_timeout",
                        "message": "%s" % exc,
                    }
                )
                return
            except Exception as exc:  # noqa: BLE001
                await self._broadcast(
                    {
                        "type": "error",
                        "message": "%s: %s" % (type(exc).__name__, exc),
                    }
                )
                return
            finally:
                if self._stop_event is not None:
                    self._stop_event.clear()
            self.history = result.messages
            await self._broadcast(
                {
                    "type": "done",
                    "text": result.final_text,
                    "tool_calls": len(result.tool_calls),
                    "turns": result.turns,
                    "model": self.config.model,
                }
            )

    async def _screenshot(self, websocket, message: dict[str, Any]) -> None:
        image_base64 = str(message.get("image_base64", "") or "")
        if not image_base64:
            await self._send(
                websocket, {"type": "error", "message": "screenshot has no image_base64"}
            )
            return
        try:
            png_bytes = base64.b64decode(image_base64)
        except Exception:
            await self._send(
                websocket, {"type": "error", "message": "invalid screenshot base64"}
            )
            return

        screenshot_dir = self.config.screenshot_dir_path()
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = screenshot_dir / ("screenshot_%s.png" % stamp)
        path.write_bytes(png_bytes)
        await self._broadcast(
            {"type": "image", "path": str(path), "tool": "dock_screenshot"}
        )

        description: str | None = None
        if self.config.vision_enabled:
            try:
                description = await describe_png(png_bytes, self.config)
            except Exception as exc:  # noqa: BLE001
                await self._broadcast(
                    {
                        "type": "status",
                        "message": "vision description failed: %s" % exc,
                        "level": "warn",
                    }
                )
            if description:
                await self._broadcast({"type": "vision", "text": description})
                self.history = DeepSeekGodotAgent.prepare_for_new_user_turn(self.history)
                self.history.append(
                    {
                        "role": "user",
                        "content": (
                            "[The user attached a Godot screenshot. Vision-model "
                            "description:\n%s]"
                        )
                        % description,
                    }
                )
                await self._broadcast(
                    {
                        "type": "status",
                        "message": "screenshot captured and described; ask your next question.",
                        "level": "ok",
                    }
                )
                return

        self.history = DeepSeekGodotAgent.prepare_for_new_user_turn(self.history)
        self.history.append(
            {
                "role": "user",
                "content": (
                    "[The user attached a Godot screenshot. It is displayed in the "
                    "dock. No vision model is configured, so describe the next step "
                    "from the user's request.]"
                ),
            }
        )
        await self._broadcast(
            {
                "type": "status",
                "message": "screenshot captured (no vision model configured; it is displayed in the dock).",
                "level": "warn",
            }
        )

    # ------------------------------------------------------------------
    async def _agent_output(self, kind: str, text: str) -> None:
        await self._broadcast({"type": kind, "text": text})

    async def _broadcast_loop(self) -> None:
        queue = self.provider.event_queue
        while True:
            event = await queue.get()
            await self._broadcast(event)

    async def _broadcast(self, event: dict[str, Any]) -> None:
        if not self.clients:
            return
        stale = []
        for client in list(self.clients):
            try:
                await client.send(json.dumps(event, ensure_ascii=False, default=str))
            except Exception:
                stale.append(client)
        for client in stale:
            self.clients.discard(client)

    async def _send(self, websocket, event: dict[str, Any]) -> None:
        try:
            await websocket.send(json.dumps(event, ensure_ascii=False, default=str))
        except Exception:
            self.clients.discard(websocket)
