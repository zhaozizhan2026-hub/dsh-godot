"""Godot-friendly launcher for the dsh-godot service.

Run with the project's Python:

    .venv\\Scripts\\python.exe run_dsh_godot.py serve
"""

from dsh_godot.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
