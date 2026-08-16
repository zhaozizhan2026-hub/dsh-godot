@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_dsh_godot.py serve
) else (
    python run_dsh_godot.py serve
)
pause
