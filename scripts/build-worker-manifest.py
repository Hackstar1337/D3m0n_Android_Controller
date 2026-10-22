#!/usr/bin/env python3
"""Freeze the reviewed Mobilerun source subset into a deterministic manifest."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from ops.worker_isolation import build_manifest  # noqa: E402

REVIEWED_CLOSURE = (
    "ops/__init__.py",
    "ops/platform_config.py",
    "ops/windows_security.py",
    "ops/worker_isolation.py",
    "ops/ai_fleet_sidecar.py",
    "ops/ai_fleet_jobs.py",
    "ops/ai_fleet_settings.py",
    "ops/ai_model_policy.py",
    "ai_contracts/__init__.py",
    "ai_contracts/models.py",
    "gitd/__init__.py",
    "gitd/services/__init__.py",
    "gitd/services/sdk_worker_client.py",
    "gitd/services/sdk_worker_protocol.py",
    "gitd/services/mobilerun_runtime.py",
    "gitd/services/native_observation.py",
    "gitd/services/sdk_gateway_llm.py",
    "gitd/services/cloud_gateway.py",
)

parser = argparse.ArgumentParser(description="freeze the reviewed worker source manifest")
parser.add_argument("--source-root", type=Path, default=ROOT)
parser.add_argument("--output", type=Path, default=ROOT / "runtime/host-isolation/source-manifest.sha256")
args = parser.parse_args()
print(build_manifest(args.source_root, REVIEWED_CLOSURE, args.output))
