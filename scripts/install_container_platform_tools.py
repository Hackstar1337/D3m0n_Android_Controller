"""Backward-compatible container entry point for the shared installer."""
from __future__ import annotations

from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from ops.compatibility import platform_tools_spec  # noqa: E402
from scripts.install_platform_tools import install  # noqa: E402

DESTINATION = Path("/opt/android")


def main() -> None:
    # Resolve the spec before opening the network so unsupported architectures
    # fail before an artifact can be downloaded or installed.
    spec = platform_tools_spec("linux", platform.machine())
    target = install("linux", platform.machine(), DESTINATION)
    print(f"Installed official Platform-Tools {spec['version']}; SHA-256 verified at {target}")


if __name__ == "__main__":
    main()
