"""Read-only installation diagnostics for Windows and Linux.

Only executable version probes are run.  In particular this module never runs
``adb devices`` or any provider/network discovery operation.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import platform
import shutil
import subprocess
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPPORTED = {(3, 10), (3, 11), (3, 12)}


def _version(command: str, *args: str) -> tuple[str, str]:
    path = shutil.which(command)
    if not path:
        return "missing", "not found on PATH"
    try:
        result = subprocess.run([path, *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return "error", str(exc)
    output = (result.stdout or result.stderr).strip().splitlines()
    return ("present" if result.returncode == 0 else "error", output[0][:160] if output else f"exit {result.returncode}")


def _node_supported(detail: str) -> bool:
    match = re.search(r"v?(\d+)\.(\d+)", detail)
    if not match:
        return False
    major, minor = int(match.group(1)), int(match.group(2))
    return (major == 20 and minor >= 19) or major >= 22


def collect() -> dict[str, object]:
    py = (sys.version_info.major, sys.version_info.minor)
    checks: dict[str, dict[str, object]] = {
        "python": {"status": "present" if py in SUPPORTED else "unsupported", "version": platform.python_version(), "supported": sorted(f"{a}.{b}" for a, b in SUPPORTED)},
    }
    for name, args, optional in (("node", ("--version",), False), ("npm", ("--version",), False), ("adb", ("--version",), False), ("scrcpy", ("--version",), True), ("ffmpeg", ("-version",), True), ("ffprobe", ("-version",), True)):
        status, detail = _version(name, *args)
        if name == "node" and status == "present" and not _node_supported(detail):
            status = "unsupported"
            detail = f"{detail} (need ^20.19.0 or >=22.12.0)"
        checks[name] = {"status": "optional-missing" if optional and status == "missing" else status, "detail": detail, "optional": optional}
    for module in ("ops", "ai_contracts"):
        try:
            importlib.import_module(module)
            checks[f"import:{module}"] = {"status": "present"}
        except Exception as exc:
            checks[f"import:{module}"] = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
    index = ROOT / "frontend" / "dist" / "index.html"
    checks["frontend"] = {"status": "present" if index.is_file() else "missing", "path": "frontend/dist/index.html"}
    jar = ROOT / "ops" / "scrcpy-server-v5.0.1.jar"
    if jar.is_file():
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        checks["scrcpy-asset"] = {"status": "present", "bytes": jar.stat().st_size, "sha256": digest, "version": "5.0.1"}
    else:
        checks["scrcpy-asset"] = {"status": "missing", "detail": "bundled ops/scrcpy-server-v5.0.1.jar is required for control/stream paths"}
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only D3m0n installation doctor")
    parser.add_argument("--json", action="store_true", help="emit machine-readable diagnostics")
    parser.add_argument("--dry-run", action="store_true", help="document read-only mode; same checks, no writes")
    parser.add_argument("--check", action="store_true", help="explicit check alias")
    args = parser.parse_args(argv)
    checks = collect()
    if args.json:
        print(json.dumps(checks, indent=2, sort_keys=True))
    else:
        print(f"D3m0n doctor (read-only; platform={platform.system()})")
        for name, data in checks.items():
            detail = data.get("detail", data.get("version", ""))
            print(f"{data['status']:>16}  {name}: {detail}")
        print("No adb devices query, network scan, provider call, secret read, or file write was performed.")
    bad = [name for name, data in checks.items() if data.get("status") in {"missing", "error", "unsupported"} and not data.get("optional")]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
