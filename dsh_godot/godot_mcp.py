"""Async MCP client for the Godot AI MCP server."""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

try:  # current MCP SDK naming
    from mcp.client.streamable_http import streamable_http_client as _streamable_http_client
except ImportError:  # pragma: no cover - older 1.x releases
    from mcp.client.streamable_http import streamablehttp_client as _streamable_http_client

from .config import BridgeConfig


class GodotMcpClient:
    """Owns one MCP session to Godot AI.

    Supported transports:
      - ``http``:  streamable HTTP, the transport Godot AI writes for its
        modern MCP clients.  Default URL: ``http://127.0.0.1:8000/mcp``.
      - ``sse``:   legacy HTTP+SSE transport.
      - ``stdio``: spawn ``godot-ai attach`` (or a custom command) locally.
    """

    def __init__(self, config: BridgeConfig):
        self.config = config
        self.session: ClientSession | None = None
        self._stack: contextlib.AsyncExitStack | None = None

    async def __aenter__(self) -> "GodotMcpClient":
        self._stack = contextlib.AsyncExitStack()
        try:
            read_stream, write_stream = await self._open_transport(self._stack)
            session_context = ClientSession(read_stream, write_stream)
            self.session = await self._stack.enter_async_context(session_context)
            await self.session.initialize()
            return self
        except BaseException:
            await self._stack.aclose()
            self.session = None
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.session = None
        if self._stack is not None:
            stack = self._stack
            self._stack = None
            await stack.aclose()

    async def close(self) -> None:
        await self.__aexit__(None, None, None)

    async def _open_transport(self, stack: contextlib.AsyncExitStack):
        transport = self.config.transport
        if transport == "stdio":
            entered = await stack.enter_async_context(self._stdio_context())
        elif transport == "sse":
            entered = await stack.enter_async_context(self._sse_context())
        else:
            entered = await stack.enter_async_context(self._http_context())
        # Different MCP SDK versions yield either (read, write) or
        # (read, write, get_session_id).  Only the streams matter here.
        if isinstance(entered, tuple):
            return entered[0], entered[1]
        read_stream, write_stream = entered
        return read_stream, write_stream

    @asynccontextmanager
    async def _http_context(self):
        timeout = httpx.Timeout(
            self.config.mcp_timeout,
            read=self.config.mcp_sse_read_timeout,
        )
        client = httpx.AsyncClient(timeout=timeout)
        try:
            async with _streamable_http_client(
                self.config.godot_url,
                http_client=client,
                terminate_on_close=True,
            ) as streams:
                yield streams
        finally:
            await client.aclose()

    def _sse_context(self):
        return sse_client(
            self.config.godot_url,
            timeout=self.config.mcp_timeout,
            sse_read_timeout=self.config.mcp_sse_read_timeout,
        )

    def _stdio_context(self):
        params = StdioServerParameters(
            command=self.config.stdio_command,
            args=self.config.stdio_args_effective(),
            env=self.config.env_for_subprocess(),
        )
        return stdio_client(params)

    async def list_tools(self) -> list[Any]:
        if self.session is None:
            raise RuntimeError("GodotMcpClient is not connected.")
        result = await self.session.list_tools()
        tools = getattr(result, "tools", None)
        if tools is None:
            return []
        return list(tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self.session is None:
            raise RuntimeError("GodotMcpClient is not connected.")
        return await self.session.call_tool(name, arguments or {})
