"""Dependency-free installation compatibility and read-only ADB capability checks."""
from __future__ import annotations

import json
import platform as _platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

MANIFEST_PATH = Path(__file__).with_name("install-compatibility.json")


def load_compatibility(path: str | Path | None = None) -> dict[str, Any]:
    manifest_path = Path(path) if path is not None else MANIFEST_PATH
    with manifest_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"unsupported compatibility manifest: {manifest_path}")
    for key in ("python", "node", "platform_tools", "adb", "diagnostic_profiles"):
        if key not in data:
            raise ValueError(f"compatibility manifest is missing {key!r}")
    return data


def _version_tuple(value: Any) -> tuple[int, int, int] | None:
    if isinstance(value, (tuple, list)):
        try:
            values = [int(x) for x in value[:3]]
        except (TypeError, ValueError):
            return None
        if len(values) == 2:
            values.append(0)
        return tuple(values[:3]) if len(values) >= 3 else None
    match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?", str(value or ""))
    if not match:
        return None
    return tuple(int(group or 0) for group in match.groups())


def _min_version(value: Any) -> tuple[int, int, int] | None:
    return _version_tuple(value)


def python_supported(version: Any, guided: bool = True) -> bool:
    """Return whether *version* satisfies the Python policy in the manifest."""
    parsed = _version_tuple(version)
    if parsed is None:
        return False
    policy = load_compatibility()["python"]
    guided_versions = {_version_tuple(item)[:2] for item in policy.get("guided", ()) if _version_tuple(item)}
    if guided and guided_versions:
        return parsed[:2] in guided_versions
    minimum = _min_version(policy.get("package_min"))
    maximum = _min_version(policy.get("package_max"))
    if minimum and parsed < minimum:
        return False
    if maximum and parsed > (maximum[0], maximum[1], 99):
        return False
    return True


def node_supported(version_text: Any) -> bool:
    """Return whether Node satisfies one of the manifest's supported ranges."""
    parsed = _version_tuple(version_text)
    if parsed is None:
        return False
    policy = load_compatibility()["node"]
    for item in policy.get("ranges", ()):
        text = str(item).replace(" ", "")
        match = re.fullmatch(r"(\d+)\.(\d+)\.0-(\d+)\.x", text)
        if match:
            major, minimum_minor, maximum_major = map(int, match.groups())
            if parsed[0] == major and parsed[1] >= minimum_minor and parsed[0] <= maximum_major:
                return True
            continue
        match = re.fullmatch(r">=(\d+)\.(\d+)\.(\d+)", text)
        if match and parsed >= tuple(map(int, match.groups())):
            return True
    return False


def _has_command(help_text: str, command: str) -> bool:
    return bool(re.search(rf"(?im)(?<![a-z0-9_-]){re.escape(command)}(?![a-z0-9_-])", help_text))


def adb_capabilities(version_output: str, help_output: str) -> dict[str, Any]:
    """Interpret version/help text without starting an ADB server or contacting phones."""
    version_text = str(version_output or "")
    help_text = str(help_output or "")
    manifest = load_compatibility()
    groups = manifest["adb"].get("required_commands", {})
    parsed = _version_tuple(version_text)
    has_adb_version = bool(parsed and re.search(r"(?i)android\s+debug\s+bridge", version_text))
    package_match = re.search(r"(?im)^\s*version\s+([0-9]+\.[0-9]+\.[0-9]+)", version_text)
    package_version = package_match.group(1) if package_match else None
    protocol_match = re.search(r"(?im)protocol\s+version\s+([0-9.]+)", version_text)
    protocol_version = protocol_match.group(1) if protocol_match else (".".join(map(str, parsed)) if parsed else None)
    group_present = {
        name: bool(required) and all(_has_command(help_text, command) for command in required)
        for name, required in groups.items()
    }
    usb_control = group_present.get("usb_control", False)
    tcp_control = group_present.get("tcp_control", False)
    pairing = group_present.get("wireless_pairing", False)
    wireless_pairing = has_adb_version and pairing
    present = {
        "usb control": has_adb_version and usb_control,
        "TCP control": has_adb_version and tcp_control,
        "wireless pairing (pair + mdns)": wireless_pairing,
    }
    return {
        "available": has_adb_version,
        "version": package_version,
        "protocol_version": protocol_version,
        "usb_control": present["usb control"],
        "tcp_control": present["TCP control"],
        "usb_tcp_control": present["usb control"] and present["TCP control"],
        "pair": _has_command(help_text, "pair") if "wireless_pairing" in groups else False,
        "mdns": _has_command(help_text, "mdns") if "wireless_pairing" in groups else False,
        "wireless_pairing": wireless_pairing,
        "required_for_install": has_adb_version and all(present.values()),
        "missing": [name for name, is_present in present.items() if not is_present],
    }


def _run_read_only(path: str, argument: str, timeout: float = 10) -> tuple[int, str, str]:
    try:
        completed = subprocess.run([path, argument], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def probe_adb(path: str = "adb") -> dict[str, Any]:
    """Run only ``adb version`` and ``adb help``; never starts a server."""
    resolved = shutil.which(path) if not Path(path).is_file() else str(Path(path))
    if not resolved:
        return {"path": path, "available": False, "error": "executable not found", "capabilities": adb_capabilities("", "")}
    version_rc, version_stdout, version_stderr = _run_read_only(resolved, "version")
    help_rc, help_stdout, help_stderr = _run_read_only(resolved, "help")
    version_output = "\n".join(part for part in (version_stdout, version_stderr) if part)
    help_output = "\n".join(part for part in (help_stdout, help_stderr) if part)
    capabilities = adb_capabilities(version_output, help_output)
    return {"path": resolved, "available": capabilities["available"] and version_rc == 0, "version_returncode": version_rc, "help_returncode": help_rc, "version_output": version_output.strip(), "help_output": help_output.strip(), "capabilities": capabilities, "error": None if version_rc == 0 and help_rc == 0 else "version/help probe failed"}


def _normal_platform(value: str) -> str:
    normalized = str(value).lower().replace(" ", "_")
    return {"win": "windows", "win32": "windows", "windows": "windows", "gnu/linux": "linux"}.get(normalized, normalized)


def _normal_arch(value: str) -> str:
    normalized = str(value).lower().replace("-", "_")
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(normalized, normalized)


def platform_tools_spec(platform: str, arch: str) -> dict[str, Any]:
    manifest = load_compatibility()
    platform_name, arch_name = _normal_platform(platform), _normal_arch(arch)
    platform_specs = manifest["platform_tools"].get(platform_name)
    if not isinstance(platform_specs, Mapping) or arch_name not in platform_specs:
        supported = sorted(platform_specs) if isinstance(platform_specs, Mapping) else []
        raise ValueError(f"unsupported Platform-Tools target {platform_name}/{arch_name}; supported architectures for {platform_name}: {', '.join(supported) or 'none'}")
    spec = dict(platform_specs[arch_name])
    if spec.get("automatic_download") and (not spec.get("verified") or not re.fullmatch(r"[0-9a-f]{64}", str(spec.get("sha256", "")))):
        raise ValueError(f"automatic Platform-Tools download is not checksum-verified for {platform_name}/{arch_name}")
    spec.update({"platform": platform_name, "arch": arch_name, "version": manifest["platform_tools"]["version"]})
    return spec


def diagnostic_profile(name: str) -> dict[str, Any]:
    profiles = load_compatibility()["diagnostic_profiles"]
    if name not in profiles:
        raise ValueError(f"unknown diagnostic profile {name!r}; choose one of {', '.join(sorted(profiles))}")
    return dict(profiles[name])


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Read-only D3m0n compatibility probes")
    parser.add_argument("--adb", default=None)
    parser.add_argument("--platform", choices=tuple(load_compatibility()["platform_tools"]), help="show archive specification")
    parser.add_argument("--arch", default=_platform.machine())
    args = parser.parse_args()
    if args.adb:
        print(json.dumps(probe_adb(args.adb), indent=2, sort_keys=True))
    elif args.platform:
        print(json.dumps(platform_tools_spec(args.platform, args.arch), indent=2, sort_keys=True))
    else:
        print(json.dumps(load_compatibility(), indent=2, sort_keys=True))
