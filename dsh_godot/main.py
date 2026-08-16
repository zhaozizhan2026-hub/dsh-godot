"""Backwards-compatible alias for ``deepseek_bridge.cli``.

Older versions of the bridge ran via ``python -m deepseek_bridge.main``.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
