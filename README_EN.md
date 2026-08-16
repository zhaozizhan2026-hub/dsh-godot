# DSH Godot Plugin

Use DeepSeek `dsh` natively inside the Godot editor: chat, live reasoning, tool calls, file editing, web search, screenshots, and more.

## What This Is

A **Godot editor plugin + local Python service**:

- `addons/dsh_godot/` — Godot editor dock plugin
- `dsh_godot/` — Python service (WebSocket + DeepSeek Harness agent)
- `run_dsh_godot.py` — Python service launcher

No demo game is included. Install it into any Godot 4.5+ project.

## Features

- In-editor DSH dock
- DeepSeek V4 long thinking and streaming reasoning
- PTC (parallel tool calling)
- Model selection, full-power / eco modes, stop button
- Direct project file read/write with automatic Godot filesystem refresh
- Web search and web page fetching
- Optional vision description for screenshots
- Optional Godot AI MCP integration
- Collapsible thinking chain and tool results

## Installation

```powershell
# 1. Copy addons/dsh_godot into your Godot project's addons folder.
# 2. Copy these files into your Godot project root:
#    dsh_godot/
#    run_dsh_godot.py
#    requirements.txt
#    .env.example

# 3. Create a Python environment and install dependencies.
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 4. Configure DeepSeek.
Copy-Item .env.example .env
# Edit .env and set DEEPSEEK_API_KEY.

# 5. Open Godot and enable the plugin:
# Project > Project Settings > Plugins > DSH Godot
```

After opening Godot, the DSH Godot dock will start and connect to the local service automatically.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | - | DeepSeek API key |
| `DEEPSEEK_MODEL` | `deepseek-v4-pro` | Model id |
| `DEEPSEEK_THINKING` | `true` | Long thinking mode |
| `DEEPSEEK_MAX_TOKENS` | `0` | 0 = no bridge-imposed limit |
| `DSH_WEB_ENABLED` | `true` | Web tools |
| `DSH_PARALLEL_TOOLS` | `true` | Parallel tool calling |
| `BRIDGE_STREAM` | `true` | Stream reasoning to the dock |
| `DSH_VISION_API_KEY` | - | Optional vision model key |
| `DSH_VISION_MODEL` | - | Optional vision model id |

## Command Line

```powershell
.\.venv\Scripts\python.exe run_dsh_godot.py serve
.\.venv\Scripts\python.exe run_dsh_godot.py chat "Create scripts/player.gd"
.\.venv\Scripts\python.exe run_dsh_godot.py list-tools
.\.venv\Scripts\python.exe run_dsh_godot.py doctor
```

## Security

- API keys stay in a local `.env` file.
- The repository ships only `.env.example`.
- File tools are restricted to the project root directory.

## License

MIT
