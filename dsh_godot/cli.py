"""`dsh-godot` command line interface.

    dsh-godot serve          # start the WebSocket service used by the Godot dock
    dsh-godot chat "prompt"  # one-shot terminal mode
    dsh-godot list-tools     # print tools available to the agent
    dsh-godot doctor         # check environment + DeepSeek connectivity
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .agent import DeepSeekGodotAgent
from .config import BridgeConfig, load_dotenv_safe
from .godot_mcp import GodotMcpClient
from .server import DshGodotService
from .tools import CompositeToolProvider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh-godot",
        description="DeepSeek dsh <-> Godot editor end-to-end coding agent.",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the WebSocket service for the Godot dock.")
    _add_common_args(serve)
    serve.add_argument("--host", default=None, help="WebSocket host (default 127.0.0.1).")
    serve.add_argument("--port", type=int, default=None, help="WebSocket port (default 9600).")

    chat = sub.add_parser("chat", help="One-shot terminal chat with project/MCP tools.")
    _add_common_args(chat)
    chat.add_argument("prompt", nargs="+", help="Prompt text.")

    tools = sub.add_parser("list-tools", help="List MCP + local project tools.")
    _add_common_args(tools)

    doctor = sub.add_parser("doctor", help="Check environment and a 1-token DeepSeek call.")
    _add_common_args(doctor)

    sub.add_parser("version", help="Print version.")
    return parser


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--thinking", action="store_true", default=None)
    parser.add_argument("--godot-url", "--url", dest="godot_url", default=None)
    parser.add_argument(
        "--transport", choices=("http", "sse", "stdio"), default=None
    )
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--tool-include", action="append", default=[])
    parser.add_argument("--tool-exclude", action="append", default=[])
    parser.add_argument("--max-tool-turns", type=int, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument(
        "--web", action="store_true", default=None, help="Enable web_search/web_fetch."
    )
    parser.add_argument(
        "--no-web", action="store_true", default=None, help="Disable web access."
    )
    parser.add_argument("--dry-run", action="store_true", default=None)
    parser.add_argument("--verbose", action="store_true", default=None)


def _flatten_patterns(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def _apply_args(config: BridgeConfig, args: argparse.Namespace) -> BridgeConfig:
    config.model = args.model or config.model
    if args.api_key is not None:
        config.api_key = args.api_key
    config.base_url = args.base_url or config.base_url
    if args.thinking is not None:
        config.thinking = args.thinking
    config.godot_url = args.godot_url or config.godot_url
    if args.transport:
        config.transport = args.transport
    if args.project_root:
        config.project_root = args.project_root
    config.tool_include = _flatten_patterns(args.tool_include) + config.tool_include
    config.tool_exclude = _flatten_patterns(args.tool_exclude) + config.tool_exclude
    if args.max_tool_turns is not None:
        config.max_tool_turns = args.max_tool_turns
    if args.max_tokens is not None:
        config.max_tokens = args.max_tokens
    if args.web is not None and args.web:
        config.web_enabled = True
    if args.no_web is not None and args.no_web:
        config.web_enabled = False
    if args.dry_run is not None:
        config.dry_run = args.dry_run
    if args.verbose is not None:
        config.verbose = args.verbose
    if getattr(args, "host", None):
        config.ws_host = args.host
    if getattr(args, "port", None):
        config.dock_port = args.port
    config.validate()
    return config


def _build_harness(config: BridgeConfig):
    if not config.api_key:
        raise ValueError("DEEPSEEK_API_KEY is not set. Put it in .env or pass --api-key.")
    from deepseek_harness import DeepSeekHarness

    return DeepSeekHarness(
        api_key=config.api_key,
        base_url=config.base_url,
        salvage_tool_calls=config.salvage_tool_calls,
        normalize_cache_fields=config.normalize_cache_fields,
        warn_on_missing_reasoning=config.warn_on_missing_reasoning,
        disable_thinking_by_default=not config.thinking,
        raw_dump_path=config.raw_dump_path or None,
    )


async def _serve(config: BridgeConfig) -> int:
    service = DshGodotService(config)
    await service.run()
    return 0


async def _chat(config: BridgeConfig, prompt: str) -> int:
    harness = _build_harness(config)
    provider = CompositeToolProvider(config)
    await provider.start()
    try:
        agent = DeepSeekGodotAgent(config, harness, provider, output=_terminal_output)
        result = await agent.run(prompt)
        print("\n" + (result.final_text or "(no answer)") + "\n")
        return 0 if not result.stopped_by_turn_limit else 2
    finally:
        await provider.close()


async def _terminal_output(kind: str, text: str) -> None:
    if kind in {"info", "tool_call", "tool_result", "reasoning"}:
        print("[%s] %s" % (kind, text), file=sys.stderr, flush=True)


async def _list_tools(config: BridgeConfig) -> int:
    provider = CompositeToolProvider(config)
    await provider.start()
    try:
        tools = await provider.list_tools()
        print(json.dumps(tools, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        await provider.close()


async def _doctor(config: BridgeConfig) -> int:
    print("project root :", config.project_root_path())
    print("deepseek url :", config.base_url)
    print("model        :", config.model)
    print("godot mcp    :", config.godot_url)
    if not config.api_key:
        print("DEEPSEEK_API_KEY: missing", file=sys.stderr)
        return 2
    harness = _build_harness(config)
    try:
        # Doctor always runs thinking-off so the 1-token probe is not eaten
        # by reasoning tokens, independent of the project's .env mode.
        out = harness.chat(
            model=config.model,
            messages=[{"role": "user", "content": "Reply with exactly OK."}],
            max_tokens=32,
            extra_body={"thinking": {"type": "disabled"}},
        )
    except Exception as exc:  # noqa: BLE001
        print("live call FAILED: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    content = out.get("message", {}).get("content")
    print("live call    :", repr(content))
    return 0 if content and "OK" in str(content) else 2


async def amain(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None or args.command == "version":
        from . import __version__

        print("dsh-godot %s" % __version__)
        parser.print_help() if args.command is None else None
        return 0

    project_root_arg = getattr(args, "project_root", None)
    if project_root_arg:
        load_dotenv_safe(Path(project_root_arg) / ".env")
    load_dotenv_safe()
    config = _apply_args(BridgeConfig.from_env(), args)

    if args.command == "serve":
        return await _serve(config)
    if args.command == "chat":
        return await _chat(config, " ".join(args.prompt))
    if args.command == "list-tools":
        return await _list_tools(config)
    if args.command == "doctor":
        return await _doctor(config)
    return 2


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:
        print("\ndsh-godot interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print("[dsh-godot error] %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
