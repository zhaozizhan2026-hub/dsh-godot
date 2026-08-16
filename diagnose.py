"""DSH Godot installation / startup diagnostics."""

import importlib.util
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("[OK]   " if ok else "[FAIL] ") + label + ("" if ok else " -> " + detail))
    return ok


def main() -> int:
    print("project root:", ROOT)
    all_ok = True

    all_ok &= check("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        env_present = (ROOT / ".env").exists()
        all_ok &= check(".env file", env_present, "copy .env.example to .env")
    except Exception:
        pass

    key = os.getenv("DEEPSEEK_API_KEY", "")
    if key:
        masked = key[:4] + "..." + key[-4:] if len(key) > 10 else "****"
        check("DEEPSEEK_API_KEY", True, "configured: " + masked)
    else:
        all_ok = False
        check("DEEPSEEK_API_KEY", False, "missing. Paste it in the DSH Godot dock and click Save Key.")

    spec = importlib.util.find_spec("deepseek_harness")
    all_ok &= check("deepseek-harness installed", spec is not None, "pip install -r requirements.txt")

    spec = importlib.util.find_spec("websockets")
    all_ok &= check("websockets installed", spec is not None, "pip install -r requirements.txt")

    spec = importlib.util.find_spec("mcp")
    all_ok &= check("mcp installed", spec is not None, "pip install -r requirements.txt")

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 9610))
        check("port 9610 free", True)
    except OSError as exc:
        all_ok = False
        check("port 9610 free", False, "another dsh service is already running: " + str(exc))

    try:
        import dsh_godot  # noqa: F401
        check("dsh_godot import", True)
    except Exception as exc:
        all_ok = False
        check("dsh_godot import", False, str(exc))

    print("\nDIAGNOSIS:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
