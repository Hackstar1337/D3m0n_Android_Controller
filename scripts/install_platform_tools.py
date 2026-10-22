"""Install a verified Google Android Platform-Tools archive."""
from __future__ import annotations

import argparse
import hashlib
import io
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from ops.compatibility import platform_tools_spec, probe_adb  # noqa: E402

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
TRANSIENT_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
DEFAULT_DOWNLOAD_ATTEMPTS = 3


def read_archive(
    url: str,
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
    attempts: int = DEFAULT_DOWNLOAD_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Download one archive, retrying only errors that are clearly transient.

    Checksum failures are deliberately outside this function and therefore are
    never retried as if they were transport failures.
    """
    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        try:
            with opener(url, timeout=180) as response:
                data = response.read(MAX_ARCHIVE_BYTES + 1)
            if len(data) > MAX_ARCHIVE_BYTES:
                raise RuntimeError("Platform-Tools archive exceeded the size limit")
            return data
        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT_HTTP_CODES or attempt + 1 >= attempts:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt + 1 >= attempts:
                raise
        sleep(min(2.0, 0.25 * (2**attempt)))
    raise RuntimeError(f"unable to download Platform-Tools archive: {url}")


def _source_revision(properties: str) -> str | None:
    match = None
    for line in properties.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "Pkg.Revision":
            match = value.strip()
            break
    return match


def validate_archive(data: bytes, spec: dict[str, object]) -> zipfile.ZipFile:
    expected = str(spec.get("sha256") or "").lower()
    actual = hashlib.sha256(data).hexdigest()
    if not expected or actual != expected:
        raise RuntimeError(f"Official Platform-Tools archive SHA-256 mismatch (got {actual})")
    try:
        package = zipfile.ZipFile(io.BytesIO(data))
        properties = package.read("platform-tools/source.properties").decode("utf-8")
    except (KeyError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        if "package" in locals():
            package.close()
        raise RuntimeError("Platform-Tools archive lacks a valid source.properties") from exc
    version = str(spec["version"])
    if _source_revision(properties) != version:
        package.close()
        raise RuntimeError(f"Unexpected Platform-Tools version; expected {version}")
    executable = "adb.exe" if spec.get("platform") == "windows" else "adb"
    names = {member.filename.replace("\\", "/") for member in package.infolist()}
    if f"platform-tools/{executable}" not in names:
        package.close()
        raise RuntimeError(f"Platform-Tools archive did not contain {executable}")
    for member in package.infolist():
        name = member.filename.replace("\\", "/")
        target = Path(name)
        if not name.startswith("platform-tools/") or target.is_absolute() or ".." in target.parts:
            package.close()
            raise RuntimeError(f"Unsafe Platform-Tools archive member path: {member.filename}")
        mode = (member.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            package.close()
            raise RuntimeError(f"Symlinks are not allowed in Platform-Tools archive: {name}")
    return package


def _existing_install_valid(target: Path, spec: dict[str, object]) -> bool:
    adb = target / ("adb.exe" if spec.get("platform") == "windows" else "adb")
    if not adb.is_file():
        return False
    result = probe_adb(str(adb))
    capabilities = result.get("capabilities", {})
    return bool(
        result.get("available")
        and capabilities.get("required_for_install")
        and capabilities.get("version") == str(spec.get("version"))
    )


def install_archive(data: bytes, spec: dict[str, object], destination: Path) -> Path:
    package = validate_archive(data, spec)
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "platform-tools"
    if target.exists():
        package.close()
        if _existing_install_valid(target, spec):
            return target
        raise RuntimeError(f"refusing to reuse or replace an existing Platform-Tools path that failed version/capability validation: {target}")
    temporary = Path(tempfile.mkdtemp(prefix="d3m0n-platform-tools-", dir=str(destination.parent)))
    try:
        package.extractall(temporary)
        package.close()
        extracted = temporary / "platform-tools"
        adb = extracted / ("adb.exe" if spec["platform"] == "windows" else "adb")
        if not adb.is_file():
            raise RuntimeError("Platform-Tools archive did not contain the expected adb executable")
        if spec["platform"] == "linux":
            for name in ("adb", "fastboot"):
                candidate = extracted / name
                if candidate.is_file():
                    candidate.chmod(0o755)
        os.replace(extracted, target)
        return target
    except Exception:
        package.close()
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def _validated_cache(cache_path: Path, spec: dict[str, object]) -> bytes | None:
    if not cache_path.is_file():
        return None
    try:
        data = cache_path.read_bytes()
        package = validate_archive(data, spec)
        package.close()
        return data
    except (OSError, RuntimeError, zipfile.BadZipFile):
        try:
            cache_path.unlink()
        except OSError:
            pass
        return None


def _download(spec: dict[str, object]) -> bytes:
    urls = [str(spec["url"])] + [str(url) for url in spec.get("fallback_urls", ())]
    for index, url in enumerate(urls):
        try:
            return read_archive(url)
        except urllib.error.HTTPError as exc:
            # The pinned release filename is authoritative. A manifest-listed
            # alias is only used when Google returns a permanent not-found for it.
            if exc.code == 404 and index + 1 < len(urls):
                continue
            raise
    raise RuntimeError("no Platform-Tools download URL was available")


def install(platform_name: str, arch: str, destination: Path, cache_dir: Path | None = None) -> Path:
    # Resolve and validate the target before any cache or network access. This
    # guarantees unsupported architectures fail early.
    spec = platform_tools_spec(platform_name, arch)
    destination = destination.expanduser().resolve()
    target = destination / "platform-tools"
    if target.exists() and _existing_install_valid(target, spec):
        return target
    cache_root = (cache_dir or destination.parent / ".cache" / "d3m0n-platform-tools").expanduser().resolve()
    cache_path = cache_root / str(spec["archive"])
    data = _validated_cache(cache_path, spec)
    if data is None:
        data = _download(spec)
        # Validate before writing the cache; a checksum mismatch is fatal and
        # never causes another download attempt.
        package = validate_archive(data, spec)
        package.close()
        cache_root.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, cache_path)
    return install_archive(data, spec, destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install checksum-verified Google Platform-Tools")
    parser.add_argument("--platform", default=("windows" if sys.platform.startswith("win") else "linux"), choices=("linux", "windows"))
    parser.add_argument("--arch", default=platform.machine())
    parser.add_argument("--destination", type=Path, default=Path("/opt/android"))
    parser.add_argument("--cache-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    target = install(args.platform, args.arch, args.destination, args.cache_dir)
    print(f"Installed official Platform-Tools {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
