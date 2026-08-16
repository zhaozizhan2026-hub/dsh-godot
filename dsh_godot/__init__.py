"""dsh-godot: DeepSeek dsh + Godot editor end-to-end coding agent.

The package contains:
  - the DeepSeek tool-calling agent (same ``deepseek_harness`` used by `dsh`)
  - direct project file read/write tools
  - a WebSocket service consumed by the in-editor Godot dock
"""

from .agent import AgentRunResult, DeepSeekGodotAgent, ToolCallRecord
from .config import BridgeConfig

__all__ = ["AgentRunResult", "BridgeConfig", "DeepSeekGodotAgent", "ToolCallRecord"]
__version__ = "0.2.0"
