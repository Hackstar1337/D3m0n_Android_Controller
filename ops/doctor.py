"""Read-only installation diagnostics for Windows and Linux."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .compatibility import diagnostic_profile, load_compatibility, node_supported, probe_adb, python_supported

ROOT = Path(__file__).resolve().parents[1]


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
    return node_supported(detail)


def _profile_check(status: str, *, required: bool, detail: str = "") -> dict[str, object]:
    state = "ready" if status in {"present", "ready"} else ("blocked" if required else "optional")
    return {"status": status, "state": state, "detail": detail, "optional": not required}


def collect(profile: str = "native") -> dict[str, object]:
    """Collect profile-aware read-only checks; no profile performs device discovery."""
    manifest = load_compatibility()
    profile_data = diagnostic_profile(profile)
    required = set(profile_data.get("required", ()))
    checks: dict[str, dict[str, object]] = {"profile": {"status": "present", "state": "ready", "name": profile}}
    if "python" in required:
        py = (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
        py_status = "present" if python_supported(py, guided=True) else "unsupported"
        checks["python"] = _profile_check(py_status, required=True, detail=platform.python_version())
        checks["python"].update({"version": platform.python_version(), "guided": list(manifest["python"].get("guided", ())), "package_range": [manifest["python"].get("package_min"), manifest["python"].get("package_max")]})
    else:
        checks["python"] = _profile_check("optional", required=False, detail=f"not required by {profile}")
    for name, args in (("node", ("--version",)), ("npm", ("--version",))):
        if name not in required:
            checks[name] = _profile_check("optional", required=False, detail=f"not required by {profile}")
            continue
        status, detail = _version(name, *args)
        if name == "node" and status == "present" and not _node_supported(detail):
            status = "unsupported"
            detail = f"{detail} (outside manifest ranges: {', '.join(manifest['node'].get('ranges', ()))})"
        checks[name] = _profile_check(status, required=True, detail=detail)
    if "adb" in required:
        adb = probe_adb("adb")
        capabilities = adb.get("capabilities", {})
        status = "present" if adb.get("available") and capabilities.get("required_for_install") else ("missing" if not adb.get("available") else "unsupported")
        detail = "ADB version/help probes only"
        if status == "unsupported":
            detail = "missing capabilities: " + ", ".join(capabilities.get("missing", ()))
        elif status == "missing":
            detail = str(adb.get("error") or "adb was not found")
        checks["adb"] = _profile_check(status, required=True, detail=detail)
        checks["adb"].update({"path": adb.get("path"), "capabilities": capabilities, "required_commands": manifest["adb"].get("required_commands", {})})
    else:
        checks["adb"] = _profile_check("optional", required=False, detail=f"not required by {profile}; no ADB probe run")
    for name, args in (("scrcpy", ("--version",)), ("ffmpeg", ("-version",)), ("ffprobe", ("-version",))):
        status, detail = _version(name, *args)
        checks[name] = _profile_check("optional-missing" if status == "missing" else status, required=False, detail=detail)
    for module in ("ops", "ai_contracts"):
        try:
            importlib.import_module(module)
            checks[f"import:{module}"] = _profile_check("present", required=True)
        except Exception as exc:
            checks[f"import:{module}"] = _profile_check("error", required=True, detail=f"{type(exc).__name__}: {exc}")
    index = ROOT / "frontend" / "dist" / "index.html"
    checks["frontend"] = _profile_check("present" if index.is_file() else "missing", required=True, detail="frontend/dist/index.html")
    checks["frontend"]["path"] = "frontend/dist/index.html"
    jar = ROOT / "ops" / "scrcpy-server-v5.0.1.jar"
    if jar.is_file():
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        checks["scrcpy-asset"] = _profile_check("present", required=False)
        checks["scrcpy-asset"].update({"bytes": jar.stat().st_size, "sha256": digest, "version": "5.0.1"})
    else:
        checks["scrcpy-asset"] = _profile_check("optional-missing", required=False, detail="bundled ops/scrcpy-server-v5.0.1.jar is required for control/stream paths")
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only D3m0n installation doctor")
    parser.add_argument("--json", action="store_true", help="emit machine-readable diagnostics")
    parser.add_argument("--dry-run", action="store_true", help="document read-only mode; same checks, no writes")
    parser.add_argument("--check", action="store_true", help="explicit check alias")
    parser.add_argument("--profile", choices=tuple(load_compatibility()["diagnostic_profiles"]), default="native")
    args = parser.parse_args(argv)
    checks = collect(args.profile)
    if args.json:
        print(json.dumps(checks, indent=2, sort_keys=True))
    else:
        print(f"D3m0n doctor (read-only; platform={platform.system()})")
        for name, data in checks.items():
            detail = data.get("detail", data.get("version", ""))
            print(f"{data['status']:>16}  {name}: {detail}")
        print("No adb devices query, network scan, provider call, secret read, or file write was performed.")
    bad = [name for name, data in checks.items() if data.get("state") == "blocked"]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
