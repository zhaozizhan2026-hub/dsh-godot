"""Configuration for the DeepSeek Harness <-> Godot AI bridge."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_GODOT_AI_HTTP_URL = "http://127.0.0.1:8000/mcp"
DEFAULT_GODOT_AI_HTTP_PORT = 8000
DEFAULT_GODOT_AI_WS_PORT = 9500
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"

TRUE_VALUES = {"1", "true", "yes", "on"}
VALID_TRANSPORTS = {"http", "sse", "stdio"}

DEFAULT_SYSTEM_PROMPT = (
    "You are an assistant controlling a Godot project. You can directly read "
    "and write project files with project_* tools, use web_search/web_fetch "
    "when you need current information, and use Godot AI MCP editor tools when "
    "they are online. Inspect before you edit, call the smallest tool set that "
    "satisfies the user request, and report tool errors instead of pretending "
    "they succeeded."
)


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    value = value.strip()
    if value.startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass
    # Also accept a comma-separated list for convenience.
    return _csv(value)


@dataclass
class BridgeConfig:
    """All bridge tunables.

    Values come from environment variables first, then CLI overrides are
    applied by :mod:`deepseek_bridge.cli`.
    """

    # DeepSeek Harness -----------------------------------------------------
    model: str = DEFAULT_DEEPSEEK_MODEL
    api_key: str = ""
    base_url: str = DEFAULT_DEEPSEEK_BASE_URL
    thinking: bool = False
    salvage_tool_calls: bool = True
    normalize_cache_fields: bool = True
    warn_on_missing_reasoning: bool = True
    raw_dump_path: str = ""

    # Godot AI MCP transport -------------------------------------------------
    godot_url: str = DEFAULT_GODOT_AI_HTTP_URL
    transport: str = "http"  # "http" | "sse" | "stdio"
    stdio_command: str = "godot-ai"
    stdio_args: list[str] = field(default_factory=list)
    http_port: int = DEFAULT_GODOT_AI_HTTP_PORT
    ws_port: int = DEFAULT_GODOT_AI_WS_PORT
    mcp_timeout: float = 30.0
    mcp_sse_read_timeout: float = 300.0

    # Tool surface -----------------------------------------------------------
    tool_include: list[str] = field(default_factory=list)
    tool_exclude: list[str] = field(default_factory=list)
    max_tool_turns: int = 200  # 0 means unlimited (hard safety cap still applies)
    max_tool_result_chars: int = 100_000

    # Agent behaviour ---------------------------------------------------------
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    dry_run: bool = False
    stream: bool = False
    verbose: bool = False
    max_tokens: int = 0  # 0 = omit max_tokens (no bridge-imposed output limit)
    web_enabled: bool = True
    parallel_tools: bool = True  # PTC: execute multiple requested tools concurrently
    turn_timeout_seconds: float = 600.0
    stream_chunk_timeout_seconds: float = 120.0

    # dsh Godot dock / service -------------------------------------------------
    project_root: str = ""
    ws_host: str = "127.0.0.1"
    dock_port: int = 9600
    screenshot_dir: str = ""
    vision_api_key: str = ""
    vision_base_url: str = "https://api.openai.com/v1"
    vision_model: str = ""
    vision_prompt: str = (
        "Describe this Godot editor/game screenshot for a text-only coding "
        "agent. Include visible nodes, UI text, errors, selection state, and "
        "anything useful for modifying the project. Keep it under 200 words."
    )
    vision_max_tokens: int = 512

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "BridgeConfig":
        env = dict(os.environ if environ is None else environ)
        return cls(
            model=env.get("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL).strip(),
            api_key=env.get("DEEPSEEK_API_KEY", "").strip(),
            base_url=env.get("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).strip(),
            thinking=_env_thinking(env),
            salvage_tool_calls=_env_bool(
                env, "DEEPSEEK_HARNESS_SALVAGE_TOOL_CALLS", True
            ),
            normalize_cache_fields=_env_bool(
                env, "DEEPSEEK_HARNESS_NORMALIZE_CACHE", True
            ),
            warn_on_missing_reasoning=_env_bool(
                env, "DEEPSEEK_HARNESS_WARN_MISSING_REASONING", True
            ),
            raw_dump_path=env.get("DEEPSEEK_HARNESS_RAW_DUMP_PATH", "").strip(),
            # New GODOT_AI_MCP_URL wins; the early scaffold's GODOT_AI_URL is
            # kept as a backwards-compatible alias.
            godot_url=(
                env.get("GODOT_AI_MCP_URL")
                or env.get("GODOT_AI_URL")
                or DEFAULT_GODOT_AI_HTTP_URL
            ).strip(),
            transport=env.get("GODOT_AI_MCP_TRANSPORT", "http").strip().lower(),
            stdio_command=env.get("GODOT_AI_STDIO_COMMAND", "godot-ai").strip(),
            stdio_args=_json_list(env.get("GODOT_AI_STDIO_ARGS")),
            http_port=_env_int(env, "GODOT_AI_HTTP_PORT", DEFAULT_GODOT_AI_HTTP_PORT),
            ws_port=_env_int(env, "GODOT_AI_WS_PORT", DEFAULT_GODOT_AI_WS_PORT),
            mcp_timeout=_env_float(env, "GODOT_AI_MCP_TIMEOUT", 30.0),
            mcp_sse_read_timeout=_env_float(
                env, "GODOT_AI_MCP_SSE_READ_TIMEOUT", 300.0
            ),
            tool_include=_csv(
                env.get("GODOT_AI_TOOL_INCLUDE") or env.get("ALLOWED_TOOLS")
            ),
            tool_exclude=_csv(
                env.get("GODOT_AI_TOOL_EXCLUDE") or env.get("BLOCKED_TOOLS")
            ),
            max_tool_turns=_env_int(
                env,
                "BRIDGE_MAX_TOOL_TURNS",
                _env_int(env, "MAX_TOOL_TURNS", 200),
            ),
            max_tool_result_chars=_env_int(
                env,
                "BRIDGE_MAX_TOOL_RESULT_CHARS",
                _env_int(env, "MAX_TOOL_RESULT_CHARS", 100_000),
            ),
            system_prompt=(
                env.get("BRIDGE_SYSTEM_PROMPT") or env.get("SYSTEM_PROMPT") or ""
            ).strip()
            or DEFAULT_SYSTEM_PROMPT,
            dry_run=_env_bool(env, "BRIDGE_DRY_RUN", False),
            stream=_env_bool(env, "BRIDGE_STREAM", False),
            verbose=_env_bool(env, "BRIDGE_VERBOSE", False),
            max_tokens=_env_int(env, "DEEPSEEK_MAX_TOKENS", 0),
            web_enabled=_env_bool(env, "DSH_WEB_ENABLED", True),
            parallel_tools=_env_bool(env, "DSH_PARALLEL_TOOLS", True),
            turn_timeout_seconds=_env_float(env, "DSH_TURN_TIMEOUT", 600.0),
            stream_chunk_timeout_seconds=_env_float(
                env, "DSH_STREAM_CHUNK_TIMEOUT", 120.0
            ),
            project_root=env.get("DSH_GODOT_PROJECT_ROOT", "").strip(),
            ws_host=env.get("DSH_GODOT_WS_HOST", "127.0.0.1").strip(),
            dock_port=_env_int(env, "DSH_GODOT_WS_PORT", 9600),
            screenshot_dir=env.get("DSH_GODOT_SCREENSHOT_DIR", "").strip(),
            vision_api_key=env.get("DSH_VISION_API_KEY", "").strip(),
            vision_base_url=env.get(
                "DSH_VISION_BASE_URL", "https://api.openai.com/v1"
            ).strip(),
            vision_model=env.get("DSH_VISION_MODEL", "").strip(),
            vision_prompt=env.get(
                "DSH_VISION_PROMPT",
                (
                    "Describe this Godot editor/game screenshot for a text-only "
                    "coding agent. Include visible nodes, UI text, errors, "
                    "selection state, and anything useful for modifying the "
                    "project. Keep it under 200 words."
                ),
            ),
            vision_max_tokens=_env_int(env, "DSH_VISION_MAX_TOKENS", 512),
        )

    def stdio_args_effective(self) -> list[str]:
        if self.stdio_args:
            return list(self.stdio_args)
        return [
            "attach",
            "--port",
            str(self.http_port),
            "--ws-port",
            str(self.ws_port),
        ]

    def validate(self) -> None:
        if not self.model.strip():
            raise ValueError("DeepSeek model must not be empty.")
        if self.transport not in VALID_TRANSPORTS:
            raise ValueError(
                "transport must be one of: http, sse, stdio (got %r)" % self.transport
            )
        if self.transport in {"http", "sse"} and not self.godot_url.strip():
            raise ValueError("godot_url is required for HTTP/SSE transport.")
        if self.transport == "stdio" and not self.stdio_command.strip():
            raise ValueError("stdio_command is required for stdio transport.")
        if self.max_tool_turns < 0:
            raise ValueError("max_tool_turns must be >= 0 (0 = unlimited).")
        if self.max_tokens < 0:
            raise ValueError("max_tokens must be >= 0 (0 = no explicit limit).")
        if self.max_tool_result_chars < 256:
            raise ValueError("max_tool_result_chars must be >= 256.")
        if self.mcp_timeout <= 0:
            raise ValueError("mcp_timeout must be positive.")
        if self.turn_timeout_seconds <= 0:
            raise ValueError("turn_timeout_seconds must be positive.")
        if self.stream_chunk_timeout_seconds <= 0:
            raise ValueError("stream_chunk_timeout_seconds must be positive.")
        if self.mcp_sse_read_timeout <= 0:
            raise ValueError("mcp_sse_read_timeout must be positive.")
        if self.http_port < 1 or self.http_port > 65535:
            raise ValueError("http_port must be in 1..65535.")
        if self.ws_port < 1 or self.ws_port > 65535:
            raise ValueError("ws_port must be in 1..65535.")

    def env_for_subprocess(self) -> dict[str, str]:
        """Environment passed to a stdio MCP server launched by the bridge."""
        return dict(os.environ)

    def project_root_path(self) -> Path:
        root = (self.project_root or os.getenv("DSH_GODOT_PROJECT_ROOT") or "").strip()
        if not root:
            root = str(Path.cwd())
        return Path(root).expanduser().resolve()

    def screenshot_dir_path(self) -> Path:
        if self.screenshot_dir.strip():
            return Path(self.screenshot_dir).expanduser().resolve()
        return self.project_root_path() / ".dsh_godot" / "screenshots"

    @property
    def vision_enabled(self) -> bool:
        return bool(self.vision_api_key and self.vision_model)


def _env_thinking(env: dict[str, str]) -> bool:
    """Resolve thinking mode from current or legacy environment variables."""
    if "DEEPSEEK_THINKING" in env:
        return _env_bool(env, "DEEPSEEK_THINKING", False)
    if "DEEPSEEK_DISABLE_THINKING" in env:
        # Legacy scaffold: DEEPSEEK_DISABLE_THINKING=true means thinking off.
        return not _env_bool(env, "DEEPSEEK_DISABLE_THINKING", True)
    return False


def _env_bool(env: dict[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def _env_int(env: dict[str, str], name: str, default: int) -> int:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(env: dict[str, str], name: str, default: float) -> float:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def load_dotenv_safe(path: str | Path | None = None) -> bool:
    """Load ``.env`` when python-dotenv is installed.

    The bridge deliberately does not hard-require python-dotenv: users can
    export environment variables themselves.
    """
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(dotenv_path=path, override=False)
        return True
    except Exception:
        return False
