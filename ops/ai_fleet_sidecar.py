"""One-job SDK subprocess; private pipes are its only authority channel."""
from __future__ import annotations
import asyncio
import json
import sys

MAX_LINE = 256 * 1024


def main() -> int:
    incoming, outgoing = sys.stdin, sys.stdout
    # SDK/Rich/third-party debug output is discarded by the parent, never mixed
    # with native/model frames or copied into dashboard logs/history.
    sys.stdout = sys.stderr
    if sys.argv[1:] == ["--probe"]:
        from dataclasses import asdict
        from gitd.services.mobilerun_runtime import sdk_readiness
        outgoing.write(json.dumps(asdict(sdk_readiness()), separators=(",", ":")) + "\n")
        outgoing.flush()
        return 0
    if sys.argv[1:]:
        return 2
    try:
        raw = incoming.readline(MAX_LINE + 1)
        if not raw or len(raw.encode("utf-8")) > MAX_LINE:
            return 2
        bootstrap = json.loads(raw)
        allowed = {"protocol", "nonce", "job", "child_id", "serial", "fence", "mode", "tools", "profile_metadata"}
        if not isinstance(bootstrap, dict) or set(bootstrap) != allowed or bootstrap["protocol"] != "mobilerun-pipe-v1" or bootstrap["mode"] not in {"direct", "reasoning"}:
            return 2
        from gitd.services.sdk_worker_client import execute
        asyncio.run(execute(bootstrap, incoming, outgoing))
        return 0
    except Exception as exc:
        # Only type names cross the diagnostic boundary, never exception text.
        types = []
        current = exc
        for _ in range(5):
            if current is None:
                break
            types.append(type(current).__name__)
            current = current.__cause__ or current.__context__
        outgoing.write(json.dumps({"worker_error_types": types}) + "\n")
        outgoing.flush()
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
