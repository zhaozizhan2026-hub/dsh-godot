"""Direct project-file tools plus a combined MCP/local tool provider.

The Godot dock shows the chat frontend; this module gives the DeepSeek agent
tools that write real project files immediately and route editor tools to the
Godot AI MCP server when it is online.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import fnmatch
import json
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .godot_mcp import GodotMcpClient
from .serialization import as_dict
from .web import WebTools

_LOCAL_TOOL_PREFIX = "project_"


class ProjectTools:
    """Safe read/write/search tools restricted to the Godot project root."""

    def __init__(self, config: BridgeConfig, event_queue: asyncio.Queue | None = None):
        self.config = config
        self.root = config.project_root_path()
        self.event_queue = event_queue
        self.last_changed_paths: list[str] = []

    def descriptions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "project_read_file",
                "description": (
                    "Read a UTF-8 text file from this Godot project. "
                    "`path` is relative to the project root, e.g. "
                    "'scenes/main.tscn' or 'scripts/player.gd'."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Project-relative file path."},
                        "max_chars": {
                            "type": "integer",
                            "description": "Optional cap, default 200000.",
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "project_write_file",
                "description": (
                    "Create or overwrite a UTF-8 text file in this Godot project. "
                    "Use this to write .gd scripts, .tscn scenes, .tres resources, "
                    "shaders, or JSON data. The editor dock is notified so it can "
                    "rescan and open the changed file. `path` is project-relative."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
            {
                "name": "project_list_dir",
                "description": (
                    "List files and directories under a project-relative path. "
                    "Use '.' for the project root."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Default '.'."}
                    },
                },
            },
            {
                "name": "project_search_files",
                "description": (
                    "Recursively search file names in the project with an "
                    "fnmatch pattern, e.g. '*.gd' or 'player*'. Skips .godot, "
                    ".git, .venv and caches."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "max_results": {"type": "integer", "description": "Default 300."},
                    },
                    "required": ["pattern"],
                },
            },
        ]

    def is_local_tool(self, name: str) -> bool:
        return name.startswith(_LOCAL_TOOL_PREFIX)

    def _resolve(self, raw_path: str) -> Path:
        path = Path(str(raw_path))
        if path.is_absolute():
            candidate = path
        else:
            candidate = self.root / path
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError("path escapes project root: %s" % raw_path) from exc
        return candidate

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "project_read_file":
                return await self._read_file(arguments)
            if name == "project_write_file":
                return await self._write_file(arguments)
            if name == "project_list_dir":
                return await self._list_dir(arguments)
            if name == "project_search_files":
                return await self._search(arguments)
            return _error("unknown local tool: %s" % name)
        except Exception as exc:  # noqa: BLE001 - local tool failure is tool output
            return _error("%s: %s" % (type(exc).__name__, exc))

    async def _read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve(str(arguments.get("path", "")))
        if not path.is_file():
            return _error("file not found: %s" % path)
        max_chars = int(arguments.get("max_chars", 200_000))
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated by project_read_file]"
        return _text(text)

    async def _write_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve(str(arguments.get("path", "")))
        content = str(arguments.get("content", ""))
        if not path.name or path.suffix == "":
            return _error("refusing to write a path with no file extension: %s" % path)
        abs_path = str(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except (PermissionError, OSError) as exc:
            if self.event_queue is not None:
                self.event_queue.put_nowait(
                    {
                        "type": "write_file",
                        "path": abs_path,
                        "content": content,
                        "fallback_reason": "%s: %s" % (type(exc).__name__, exc),
                    }
                )
                return _text(
                    "python sandbox denied direct write (%s); delegated the write "
                    "to the Godot dock for %s (%d chars)."
                    % (exc, abs_path, len(content))
                )
            return _error("cannot write %s: %s" % (abs_path, exc))
        self.last_changed_paths.append(abs_path)
        if self.event_queue is not None:
            self.event_queue.put_nowait(
                {"type": "files_changed", "paths": [abs_path]}
            )
        return _text("wrote %d chars to %s" % (len(content), abs_path))

    async def _list_dir(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = str(arguments.get("path", "") or ".")
        path = self._resolve(raw)
        if not path.exists():
            return _error("directory not found: %s" % path)
        if not path.is_dir():
            path = path.parent
        rows: list[dict[str, Any]] = []
        for child in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            rows.append(
                {
                    "name": child.name,
                    "kind": "file" if child.is_file() else "directory",
                    "relative_path": str(child.relative_to(self.root)),
                }
            )
        return _text(json.dumps(rows, ensure_ascii=False, indent=2))

    async def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        pattern = str(arguments.get("pattern", "") or "")
        if not pattern:
            return _error("project_search_files requires a pattern")
        max_results = int(arguments.get("max_results", 300))
        skip_parts = {".godot", ".git", ".venv", "__pycache__", ".dsh_godot"}
        matches: list[str] = []
        for path in self.root.rglob("*"):
            if len(matches) >= max_results:
                break
            if any(part in skip_parts for part in path.parts[len(self.root.parts):]):
                continue
            if path.is_file() and fnmatch.fnmatchcase(path.name, pattern):
                matches.append(str(path.relative_to(self.root)))
        return _text(
            "matched %d file(s):\n%s" % (len(matches), "\n".join(matches[:max_results]))
        )


class CompositeToolProvider:
    """Exposes local project tools plus Godot AI MCP tools through one interface."""

    def __init__(self, config: BridgeConfig, event_queue: asyncio.Queue | None = None):
        self.config = config
        self.event_queue = event_queue
        self.project_tools = ProjectTools(config, event_queue)
        self.web_tools = WebTools(config)
        self.godot: GodotMcpClient | None = None
        self.godot_online = False
        self.godot_error = ""

    async def start(self) -> None:
        self.godot = GodotMcpClient(self.config)
        try:
            await self.godot.__aenter__()
            self.godot_online = True
        except Exception as exc:  # noqa: BLE001
            self.godot_online = False
            self.godot_error = "%s: %s" % (type(exc).__name__, exc)
            await self.godot.__aexit__(None, None, None)
            self.godot = None
        if self.event_queue is not None:
            if self.godot_online:
                self.event_queue.put_nowait(
                    {
                        "type": "status",
                        "message": "Godot AI MCP connected at %s" % self.config.godot_url,
                        "level": "ok",
                    }
                )
            else:
                self.event_queue.put_nowait(
                    {
                        "type": "status",
                        "message": (
                            "Godot AI MCP offline (%s); project file tools remain available."
                            % self.godot_error
                        ),
                        "level": "warn",
                    }
                )

    async def close(self) -> None:
        if self.godot is not None:
            await self.godot.close()
            self.godot = None
        self.godot_online = False

    async def list_tools(self) -> list[Any]:
        tools: list[Any] = list(self.project_tools.descriptions())
        if self.config.web_enabled:
            tools.extend(self.web_tools.descriptions())
        if self.godot_online and self.godot is not None:
            try:
                tools.extend(await self.godot.list_tools())
            except Exception as exc:  # noqa: BLE001
                self.godot_online = False
                if self.event_queue is not None:
                    self.event_queue.put_nowait(
                        {
                            "type": "status",
                            "message": "Godot AI MCP tool list failed: %s" % exc,
                            "level": "error",
                        }
                    )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self.project_tools.is_local_tool(name):
            return await self.project_tools.call(name, arguments)
        if self.web_tools.is_web_tool(name):
            if not self.config.web_enabled:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "WEB TOOL ERROR:\nweb access is currently disabled. Enable it with /mode or the 满血 button.",
                        }
                    ],
                    "isError": True,
                }
            return await self.web_tools.call(name, arguments)
        if self.godot_online and self.godot is not None:
            result = await self.godot.call_tool(name, arguments)
            return await self._surface_images(result, name)
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "TOOL ERROR:\nGodot AI MCP is offline, so editor tool "
                        "'%s' is unavailable. Use project_* file tools instead, "
                        "or start Godot AI in Project Settings > Plugins."
                    )
                    % name,
                }
            ],
            "isError": True,
        }

    async def _surface_images(self, result: Any, tool_name: str) -> Any:
        """Save embedded MCP images and emit a dock event for the frontend."""
        item = as_dict(result)
        content = item.get("content")
        if not content:
            return result
        paths: list[str] = []
        new_content: list[Any] = []
        for block in content:
            block_dict = as_dict(block)
            new_content.append(block_dict)
            if block_dict.get("type") != "image":
                continue
            data = block_dict.get("data", "") or ""
            mime = block_dict.get("mimeType", "image/png") or "image/png"
            try:
                raw = base64.b64decode(data)
            except Exception:
                continue
            screenshot_dir = self.config.screenshot_dir_path()
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            ext = "png" if "png" in mime else ("jpg" if "jpeg" in mime else "img")
            stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = screenshot_dir / ("%s_%s.%s" % (tool_name, stamp, ext))
            path.write_bytes(raw)
            abs_path = str(path)
            paths.append(abs_path)
            if self.event_queue is not None:
                self.event_queue.put_nowait(
                    {"type": "image", "path": abs_path, "tool": tool_name}
                )
            new_content.append(
                {
                    "type": "text",
                    "text": (
                        "[image displayed in the Godot dsh dock and saved to %s]"
                        % abs_path
                    ),
                }
            )
        if paths:
            item = dict(item)
            item["content"] = new_content
            return item
        return result


def _text(value: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": value}], "isError": False}


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "TOOL ERROR:\n" + message}], "isError": True}
