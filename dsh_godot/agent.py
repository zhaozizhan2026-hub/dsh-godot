"""DeepSeek Harness agent loop that drives Godot AI MCP tools."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import BridgeConfig
from .serialization import serialize_call_tool_result, serialize_tool_exception
from .tool_schema import to_openai_tools

OutputCallback = Callable[[str, str], Awaitable[None] | None]


async def _call_output(callback: OutputCallback | None, kind: str, text: str) -> None:
    if callback is None:
        return
    result = callback(kind, text)
    if result is not None and hasattr(result, "__await__"):
        await result


@dataclass
class ToolCallRecord:
    turn: int
    index: int
    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    result_text: str
    error: str = ""


class AgentCancelled(Exception):
    """Raised inside the loop when the user presses the Stop button."""


class TurnTimeout(Exception):
    """Raised when a DeepSeek streaming turn stalls or exceeds its time budget."""


_STREAM_END = object()


@dataclass
class AgentRunResult:
    final_text: str
    messages: list[dict[str, Any]]
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    turns: int = 0
    stopped_by_turn_limit: bool = False
    cancelled: bool = False


class DeepSeekGodotAgent:
    """Runs the tool-calling loop.

    ``harness`` only needs to expose the same surface as
    ``deepseek_harness.DeepSeekHarness``:

      * ``chat(model=..., messages=..., tools=..., ...) -> dict`` where the
        dict contains ``message`` (OpenAI-shaped assistant message) and
        optionally ``usage``.
      * optionally ``stream_chat(...)`` for the ``--stream`` mode.
    """

    def __init__(
        self,
        config: BridgeConfig,
        harness: Any,
        mcp: Any,
        output: OutputCallback | None = None,
        cancel_event: asyncio.Event | None = None,
    ):
        self.config = config
        self.harness = harness
        self.mcp = mcp
        self.output = output
        self.cancel_event = cancel_event

    @staticmethod
    def prepare_for_new_user_turn(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove ``reasoning_content`` before a new user turn.

        DeepSeek only requires reasoning_content while a tool loop for the
        current user turn is still open.  Keeping it across turns bloats the
        prefix and harms cache matching.
        """
        for message in history:
            if message.get("role") == "assistant" and "reasoning_content" in message:
                message.pop("reasoning_content", None)
        return history

    async def run(
        self,
        prompt: str,
        history: list[dict[str, Any]] | None = None,
    ) -> AgentRunResult:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt must not be empty.")

        raw_tools = await self.mcp.list_tools()
        tools = _extract_tools(raw_tools)
        openai_tools = to_openai_tools(
            tools,
            include=self.config.tool_include,
            exclude=self.config.tool_exclude,
        )
        await _call_output(
            self.output,
            "info",
            "Godot AI exposed %d tool(s); %d passed the include/exclude filter."
            % (len(tools), len(openai_tools)),
        )

        messages: list[dict[str, Any]] = copy.deepcopy(history) if history else []
        if not any(message.get("role") == "system" for message in messages):
            system_prompt = self.config.system_prompt.strip()
            if system_prompt:
                messages.insert(0, {"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        records: list[ToolCallRecord] = []
        usage_total: dict[str, Any] | None = None
        final_text = ""
        stopped_by_turn_limit = False
        turn = 0
        # 0 means "no bridge-imposed limit".  The hard cap below is only a
        # runaway-loop circuit breaker, not a token limit.
        requested_limit = self.config.max_tool_turns
        hard_cap = requested_limit if requested_limit > 0 else 500

        while turn < hard_cap:
            turn += 1
            await self._check_cancelled()
            await _call_output(self.output, "turn_start", "第 %d 轮" % turn)
            response, assistant_message = await self._chat_once(
                messages, openai_tools, turn
            )
            await self._check_cancelled()
            if not isinstance(assistant_message, dict):
                raise TypeError(
                    "harness returned an assistant message of type %s; expected dict"
                    % type(assistant_message).__name__
                )

            messages.append(copy.deepcopy(assistant_message))
            usage_total = _merge_usage(usage_total, response.get("usage"))
            usage_dict = _usage_to_dict(response.get("usage"))
            if usage_dict:
                await _call_output(
                    self.output,
                    "usage",
                    json.dumps(usage_dict, ensure_ascii=False, default=str),
                )
            salvage = response.get("salvage")
            if salvage:
                await _call_output(
                    self.output,
                    "salvage",
                    json.dumps(salvage, ensure_ascii=False, default=str),
                )

            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                final_text = _message_text(assistant_message)
                break

            await _call_output(
                self.output,
                "info",
                "turn %d: model requested %d tool call(s)" % (turn, len(tool_calls)),
            )

            await self._check_cancelled()
            if self.config.parallel_tools and len(tool_calls) > 1:
                # PTC mode: DeepSeek requested several tools in one turn.
                # Execute them concurrently, then feed results back in the
                # same order so tool_call_id <-> result pairing is preserved.
                turn_records = await asyncio.gather(
                    *(
                        self._execute_tool_call(turn, index, tool_call)
                        for index, tool_call in enumerate(tool_calls)
                    )
                )
                for record in turn_records:
                    records.append(record)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": record.tool_call_id,
                            "content": record.result_text,
                        }
                    )
            else:
                for index, tool_call in enumerate(tool_calls):
                    await self._check_cancelled()
                    record = await self._execute_tool_call(turn, index, tool_call)
                    records.append(record)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": record.tool_call_id,
                            "content": record.result_text,
                        }
                    )

            final_text = _message_text(assistant_message)
            if requested_limit > 0 and turn >= requested_limit:
                stopped_by_turn_limit = True
                final_text = (
                    final_text
                    + "\n\n[bridge stopped: maximum tool turns (%d) reached]"
                    % requested_limit
                ).strip()
                break

        if turn >= hard_cap and not final_text:
            stopped_by_turn_limit = True
            final_text = "[bridge stopped by the runaway-loop safety cap (%d turns)]" % hard_cap

        return AgentRunResult(
            final_text=final_text,
            messages=messages,
            tool_calls=records,
            usage=usage_total,
            turns=turn,
            stopped_by_turn_limit=stopped_by_turn_limit,
            cancelled=False,
        )

    async def _check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise AgentCancelled("cancelled by user")

    async def _chat_once(
        self,
        messages: list[dict[str, Any]],
        openai_tools: list[dict[str, Any]],
        turn: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
        }
        if openai_tools:
            kwargs["tools"] = openai_tools
        if self.config.max_tokens > 0:
            kwargs["max_tokens"] = self.config.max_tokens
        # Always pin the DeepSeek thinking contract explicitly.  Mode changes
        # from the Godot dock can therefore take effect without rebuilding the
        # deepseek_harness client.
        kwargs["extra_body"] = {
            "thinking": {
                "type": "enabled" if self.config.thinking else "disabled"
            }
        }

        # Stream whenever the frontend asked for streaming OR cancellation is
        # possible.  The OpenAI SDK stream is a *sync* generator; running it
        # inline would block the asyncio event loop while it waits for the
        # next HTTP chunk, making Stop and new WebSocket connections hang.
        if (
            (self.config.stream or self.cancel_event is not None)
            and hasattr(self.harness, "stream_chat")
        ):
            queue = self._start_stream_thread(kwargs)
            final_event: dict[str, Any] = {}
            reasoning_buf = ""
            content_buf = ""
            last_flush = time.monotonic()
            turn_started = time.monotonic()
            while True:
                await self._check_cancelled()
                if time.monotonic() - turn_started > self.config.turn_timeout_seconds:
                    raise TurnTimeout(
                        "DeepSeek stream exceeded %.0fs turn budget"
                        % self.config.turn_timeout_seconds
                    )
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=self.config.stream_chunk_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    raise TurnTimeout(
                        "no DeepSeek stream chunk for %.0fs"
                        % self.config.stream_chunk_timeout_seconds
                    )
                if event is _STREAM_END:
                    break
                if isinstance(event, BaseException):
                    raise event
                event = (
                    event
                    if isinstance(event, dict)
                    else {"type": "unknown", "data": event}
                )
                kind = event.get("type")
                now = time.monotonic()
                if kind == "content_delta":
                    content_buf += str(event.get("data", ""))
                    if len(content_buf) >= 160 or now - last_flush >= 0.2:
                        await _call_output(self.output, "content", content_buf)
                        content_buf = ""
                        last_flush = now
                elif kind == "reasoning_delta":
                    reasoning_buf += str(event.get("data", ""))
                    if len(reasoning_buf) >= 240 or now - last_flush >= 0.2:
                        await _call_output(self.output, "reasoning", reasoning_buf)
                        reasoning_buf = ""
                        last_flush = now
                elif kind == "done":
                    final_event = event
            if reasoning_buf:
                await _call_output(self.output, "reasoning", reasoning_buf)
            if content_buf:
                await _call_output(self.output, "content", content_buf)
            message = final_event.get("message")
            if not isinstance(message, dict):
                raise RuntimeError(
                    "stream_chat ended without a 'done' event carrying an assistant message."
                )
            return final_event, message

        # Non-stream fallback also moves the blocking OpenAI call off the
        # event loop so the service keeps accepting WebSocket messages.
        response = await asyncio.to_thread(self.harness.chat, **kwargs)
        if not isinstance(response, dict):
            raise TypeError(
                "harness.chat returned %s; expected dict" % type(response).__name__
            )
        message = response.get("message")
        if not isinstance(message, dict):
            raise TypeError("harness response is missing the assistant 'message' dict.")
        if message.get("reasoning_content"):
            await _call_output(
                self.output,
                "reasoning",
                str(message.get("reasoning_content")),
            )
        return response, message

    def _start_stream_thread(self, kwargs: dict[str, Any]) -> asyncio.Queue:
        """Run DeepSeekHarness.stream_chat in a daemon thread.

        Events are forwarded to the asyncio loop through a thread-safe queue.
        If the caller stops consuming (cancel/timeout), the daemon thread is
        abandoned safely; the process remains responsive.
        """
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def worker() -> None:
            try:
                for event in self.harness.stream_chat(**kwargs):
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except BaseException as exc:  # noqa: BLE001 - forwarded to the loop
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _STREAM_END)

        thread = threading.Thread(target=worker, name="dsh-deepseek-stream", daemon=True)
        thread.start()
        return queue

    async def _execute_tool_call(
        self, turn: int, index: int, tool_call: Any
    ) -> ToolCallRecord:
        call = tool_call if isinstance(tool_call, dict) else {}
        call_id = str(call.get("id") or "call_%d_%d" % (turn, index))
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name", "") or "")
        raw_arguments = function.get("arguments", "{}")
        arguments, parse_error = _parse_tool_arguments(raw_arguments)

        await _call_output(
            self.output,
            "tool_call",
            "[turn %d.%d] %s(%s)"
            % (
                turn,
                index,
                name or "<missing name>",
                json.dumps(arguments, ensure_ascii=False, default=str),
            ),
        )

        if not name:
            result_text = (
                "TOOL ERROR:\nThe model returned a tool call without a function name."
            )
        elif parse_error:
            result_text = "TOOL ERROR:\n%s" % parse_error
        elif self.config.dry_run:
            result_text = serialize_call_tool_result(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "dry_run": True,
                                    "tool": name,
                                    "arguments": arguments,
                                    "note": "Bridge is in --dry-run mode; the tool was NOT executed.",
                                },
                                ensure_ascii=False,
                                default=str,
                            ),
                        }
                    ],
                    "isError": False,
                },
                self.config.max_tool_result_chars,
            )
        else:
            try:
                result = await self.mcp.call_tool(name, arguments)
                result_text = serialize_call_tool_result(
                    result, self.config.max_tool_result_chars
                )
            except Exception as exc:  # noqa: BLE001 - tool failure is data, not fatal
                result_text = serialize_tool_exception(
                    exc, self.config.max_tool_result_chars
                )

        await _call_output(self.output, "tool_result", _clip_for_ui(result_text))
        return ToolCallRecord(
            turn=turn,
            index=index,
            tool_call_id=call_id,
            name=name,
            arguments=arguments,
            result_text=result_text,
            error=parse_error if parse_error else "",
        )


def _clip_for_ui(text: str, limit: int = 5000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[UI truncated; full result was still sent to the model]"


def _extract_tools(raw_tools: Any) -> list[Any]:
    if raw_tools is None:
        return []
    if isinstance(raw_tools, (list, tuple)):
        return list(raw_tools)
    tools = getattr(raw_tools, "tools", None)
    if tools is not None:
        return list(tools)
    if isinstance(raw_tools, dict) and "tools" in raw_tools:
        return list(raw_tools["tools"])
    return [raw_tools]


def _parse_tool_arguments(raw: Any) -> tuple[dict[str, Any], str]:
    if raw is None or raw == "":
        return {}, ""
    if isinstance(raw, dict):
        return copy.deepcopy(raw), ""
    if not isinstance(raw, str):
        return {"value": raw}, ""
    text = raw.strip()
    if not text:
        return {}, ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, "Tool call arguments are not valid JSON: %s (raw: %.200s)" % (exc, text)
    if isinstance(parsed, dict):
        return parsed, ""
    return {"value": parsed}, ""


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if content is None:
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            names = []
            for call in tool_calls:
                if isinstance(call, dict):
                    function = call.get("function") or {}
                    names.append(str(function.get("name", "")))
                else:
                    function = getattr(call, "function", None)
                    name = getattr(function, "name", "") if function else ""
                    names.append(str(name))
            return "[assistant requested tools: %s]" % ", ".join(names)
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    out: dict[str, Any] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "estimated_cost_usd",
        "cache_hit_rate",
        "cached_tokens",
    ):
        if hasattr(usage, key):
            out[key] = getattr(usage, key)
    if hasattr(usage, "prompt_tokens_details") and getattr(
        usage, "prompt_tokens_details", None
    ):
        out["prompt_tokens_details"] = _usage_to_dict(usage.prompt_tokens_details)
    return out or {"raw": str(usage)}


def _merge_usage(
    current: dict[str, Any] | None, incoming: Any
) -> dict[str, Any] | None:
    incoming_dict = _usage_to_dict(incoming)
    if not incoming_dict:
        return current
    if current is None:
        return incoming_dict
    merged = dict(current)
    if "_turns" not in merged:
        merged["_turns"] = []
    merged["_turns"].append(incoming_dict)
    return merged
