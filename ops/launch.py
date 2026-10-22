"""Portable, authenticated launcher for the D3m0n dashboard.

This module deliberately does not discover devices, scan networks, or install
anything.  It loads local configuration before importing the application and
starts only ``ops.server:app``.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


def _load_env() -> None:
    """Load .env without replacing values already supplied by the shell."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        pass
    else:
        load_dotenv(REPO_ROOT / ".env", override=False)
    from ops.platform_config import state_dir
    private = state_dir()
    if not os.environ.get("D3M0N_DASHBOARD_AUTH_FILE", "").strip():
        os.environ["D3M0N_DASHBOARD_AUTH_FILE"] = str(private / "dashboard-auth.json")
    if not os.environ.get("DB_PATH", "").strip():
        os.environ["DB_PATH"] = str(private / "gitd.db")


def _valid_origin(value: str) -> str:
    from ops.platform_config import normalize_origin
    return normalize_origin(value)


def _origins(host: str, port: int, lan: bool) -> tuple[str, ...]:
    configured = os.environ.get("D3M0N_ALLOWED_ORIGINS", "")
    if configured:
        origins = tuple(_valid_origin(item) for item in configured.split(",") if item.strip())
        if not origins:
            raise ValueError("D3M0N_ALLOWED_ORIGINS must contain an explicit origin")
    else:
        public = os.environ.get("D3M0N_PUBLIC_ORIGIN", "")
        if public:
            origins = (_valid_origin(public),)
        elif lan and host in {"0.0.0.0", "::", "[::]", ""}:
            raise ValueError("LAN wildcard bind requires D3M0N_PUBLIC_ORIGIN or D3M0N_ALLOWED_ORIGINS")
        else:
            display_host = "127.0.0.1" if host in {"", "0.0.0.0", "::", "[::]"} else host
            origins = (f"http://{display_host}:{port}",)
    os.environ["D3M0N_ALLOWED_ORIGINS"] = ",".join(origins)
    # PUBLIC_ORIGIN describes a public HTTPS gateway, not local HTTP/LAN access.
    # The exact local origins above are sufficient for a standalone install.
    return origins


def _auth_setup() -> str:
    """Create the dashboard password only when no auth record exists."""
    from ops.dashboard_auth import AUTH_FILE, hash_password, install_hash

    if AUTH_FILE.exists():
        return f"dashboard auth preserved ({AUTH_FILE})"
    first = getpass.getpass("Create dashboard password: ")
    second = getpass.getpass("Confirm dashboard password: ")
    if first != second:
        raise ValueError("Dashboard passwords do not match")
    install_hash(AUTH_FILE, hash_password(first))
    return f"dashboard auth created at {AUTH_FILE}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the protected D3m0n dashboard")
    parser.add_argument("--lan", action="store_true", help="explicitly opt in to a LAN bind")
    parser.add_argument("--host", help="bind host; defaults to 127.0.0.1 or 0.0.0.0 with --lan")
    parser.add_argument("--port", type=int, help="bind port (default: D3M0N_PORT or 5055)")
    parser.add_argument("--setup-password", action="store_true", help="guided first-run password setup")
    parser.add_argument("--dry-run", action="store_true", help="validate configuration without importing or starting the server")
    parser.add_argument("--check", action="store_true", help="alias for --dry-run")
    parser.add_argument("--skip-install", action="store_true", help="compatibility flag; launcher never installs packages")
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = build_parser().parse_args(argv)
    host = args.host or os.environ.get("D3M0N_HOST") or os.environ.get("D3M0N_BIND_HOST") or ("0.0.0.0" if args.lan else "127.0.0.1")
    if not args.lan and host not in {"127.0.0.1", "localhost", "::1", "[::1]"}:
        raise SystemExit("Refusing non-loopback bind without explicit --lan")
    port = args.port or int(os.environ.get("D3M0N_PORT", os.environ.get("D3M0N_API_PORT", "5055")))
    if not 1 <= port <= 65535:
        raise SystemExit("Port must be between 1 and 65535")
    if args.lan and host in {"127.0.0.1", "localhost", "::1", "[::1]"}:
        raise SystemExit("--lan requires a non-loopback host or wildcard bind")
    try:
        origins = _origins(host, port, args.lan)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not (args.dry_run or args.check):
        try:
            print(_auth_setup())
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("Password setup cancelled")
    if not FRONTEND_DIST.joinpath("index.html").is_file():
        raise SystemExit(f"Frontend build missing: {FRONTEND_DIST / 'index.html'}; run npm ci --prefix frontend && npm --prefix frontend run build")
    mode = "LAN (explicit opt-in)" if args.lan else "loopback"
    print(f"D3m0n launcher: {mode} http://{host}:{port}")
    print(f"Allowed browser origins: {', '.join(origins)}")
    if args.dry_run or args.check:
        print("Dry run: no server, ADB, network discovery, provider, or phone action was started.")
        return 0
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Uvicorn is missing; run the bootstrap script first") from exc
    from ops.platform_config import ensure_private_dir, state_dir
    ensure_private_dir(state_dir())
    from ops.setup_ownership import checkout_lock
    with checkout_lock():
        from ops.server import app
        instance = os.environ.get("D3M0N_LAUNCH_INSTANCE", "")
        if instance:
            class InstanceReadiness:
                def __init__(self, app): self.app = app
                async def __call__(self, scope, receive, send):
                    async def identify(message):
                        if message.get("type") == "http.response.start" and scope.get("path") == "/api/auth/status":
                            message = dict(message)
                            message["headers"] = list(message.get("headers", [])) + [(b"x-d3m0n-instance", instance.encode("ascii"))]
                        await send(message)
                    await self.app(scope, receive, identify)
            app.add_middleware(InstanceReadiness)
        uvicorn.run(app, host=host, port=port, log_level=os.environ.get("D3M0N_LOG_LEVEL", "info"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
