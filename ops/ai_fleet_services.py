"""Concrete application services for the owner-controlled AI fleet.

This module is deliberately the only place that assembles the fleet control
plane.  The dashboard/backend process never imports Mobilerun or provider SDK
packages; SDK execution is an isolated subprocess boundary.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Protocol

from ai_contracts import Capability, ProfileRef, ProfileSnapshot, Role, TargetRef, TargetSnapshot
from ops.ai_fleet_jobs import JobState, JobStore, LeaseError
from ops.ai_fleet_settings import (
    ProfileNotFoundError, ProfileStore, ProfileValidationError, profile_snapshot,
)
from gitd.services.cloud_gateway import CloudGateway


class TargetInventory(Protocol):
    def snapshot(self) -> TargetSnapshot: ...
    def get(self, target_id: str) -> TargetRef | None: ...


class ImmutableTargetInventory:
    """A server-owned, immutable target catalog for one plan snapshot."""
    def __init__(self, targets: Mapping[str, TargetRef] | tuple[TargetRef, ...] = ()) -> None:
        rows = tuple(targets.values()) if isinstance(targets, Mapping) else tuple(targets)
        self._snapshot = TargetSnapshot(targets=rows) if rows else None
        self._by_id = {row.target_id: row for row in rows}
        self._by_serial = {row.serial: row for row in rows}

    def snapshot(self) -> TargetSnapshot:
        if self._snapshot is None:
            # Empty inventory is a truthful not-ready state; no fabricated phone
            # is ever exposed as online.
            raise RuntimeError("phone inventory is empty")
        return self._snapshot

    def get(self, target_id: str) -> TargetRef | None:
        return self._by_id.get(target_id) or self._by_serial.get(target_id)

    @classmethod
    def from_env(cls) -> "ImmutableTargetInventory":
        raw = os.environ.get("D3M0N_AI_FLEET_TARGETS", "").strip()
        if not raw:
            return cls()
        try:
            rows = json.loads(raw)
            if not isinstance(rows, list):
                raise ValueError
            targets = tuple(TargetRef.model_validate(item) for item in rows)
        except Exception as exc:
            raise RuntimeError("configured phone inventory is invalid") from exc
        return cls(targets)


class NativeTargetInventory:
    def snapshot(self):
        import asyncio, sqlite3
        from ops import fleet_control
        serials=asyncio.run(fleet_control.connected_devices())
        numbers={}
        try:
            path=fleet_control._state_db_path()
            connection=sqlite3.connect("file:"+str(path)+"?mode=ro",uri=True)
            try: numbers={row[0]:row[1] for row in connection.execute("SELECT serial,number FROM device_numbers")}
            finally: connection.close()
        except Exception: pass
        rows=tuple(TargetRef(target_id=s,serial=s,label=f"Phone {numbers[s]}" if s in numbers else s,online=True,metadata={"number":str(numbers[s])} if s in numbers else {}) for s in serials)
        if not rows: raise RuntimeError("No connected phones")
        return TargetSnapshot(targets=rows)
    def list_targets(self): return self.snapshot().targets
    def get(self,target_id): return next((r for r in self.snapshot().targets if r.target_id==target_id or r.serial==target_id),None)


class ProfileService(Protocol):
    def list_profiles(self) -> list[dict[str, Any]]: ...
    def get_profile(self, profile_id: str) -> dict[str, Any] | None: ...
    def save_profile(self, data: Mapping[str, Any], *, secret: str | None = None) -> dict[str, Any]: ...
    def update_profile(self, profile_id: str, data: Mapping[str, Any], *, secret: str | None = None) -> dict[str, Any]: ...
    def clear_secret(self, profile_id: str) -> dict[str, Any]: ...
    def revoke_profile(self, profile_id: str) -> dict[str, Any]: ...
    def role_bindings(self) -> dict[str, str]: ...
    def readiness(self, profile_id: str | None = None) -> dict[str, Any]: ...
    def snapshot_for_roles(self, bindings: Mapping[str, str]) -> ProfileSnapshot: ...


class ProfileStoreService:
    """Metadata adapter over the encrypted ProfileStore; never returns a key."""
    def __init__(self, store: ProfileStore) -> None:
        self.store = store

    @staticmethod
    def _public(value: Any) -> dict[str, Any]:
        if hasattr(value, "as_dict"):
            return value.as_dict()
        return dict(value)

    def list_profiles(self) -> list[dict[str, Any]]:
        return [self._public(row) for row in self.store.list_profiles()]

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        try:
            return self._public(self.store.get_profile(profile_id))
        except ProfileNotFoundError:
            return None

    def _payload(self, data: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"profile_id", "provider", "model", "endpoint_origin", "roles", "capabilities", "expires_at", "max_tokens", "max_calls", "max_price_usd"}
        unknown = set(data) - allowed
        if unknown:
            raise ProfileValidationError("profile metadata contains unsupported fields")
        return {key: value for key, value in data.items() if key in allowed}

    def save_profile(self, data: Mapping[str, Any], *, secret: str | None = None) -> dict[str, Any]:
        if not secret:
            raise ProfileValidationError("provider key is required for profile save")
        payload = self._payload(data)
        result = self.store.create_profile(
            payload["profile_id"], payload["provider"], payload["model"], payload["endpoint_origin"], secret,
            roles=payload.get("roles", ()), capabilities=payload.get("capabilities", ()),
            expires_at=payload.get("expires_at"), max_tokens=payload.get("max_tokens", 4096),
            max_calls=payload.get("max_calls", 1000), max_price_usd=payload.get("max_price_usd", "20"),
        )
        return self._public(result)

    def create_profile(self, data: Mapping[str, Any], *, secret: str | None = None) -> dict[str, Any]:
        """Controller-compatible create operation through encrypted storage."""
        return self.save_profile(data, secret=secret)

    def delete_profile(self, profile_id: str) -> None:
        """Revoke and clear a profile without breaking immutable freezes."""
        self.store.clear_secret(profile_id)
        self.store.revoke_profile(profile_id)

    def update_profile(self, profile_id: str, data: Mapping[str, Any], *, secret: str | None = None) -> dict[str, Any]:
        existing = self.store.get_profile(profile_id)
        payload = self._payload(dict(data, profile_id=profile_id))
        # A metadata-only patch preserves the encrypted secret without exposing it.
        if secret is None:
            try:
                secret = str(self.store.get_runtime(profile_id)["secret"])
            except Exception:
                raise ProfileValidationError("provider key is required for an unconfigured profile") from None
        result = self.store.create_profile(
            profile_id, payload.get("provider", existing.provider), payload.get("model", existing.model),
            payload.get("endpoint_origin", existing.endpoint_origin), secret,
            roles=payload.get("roles", existing.roles), capabilities=payload.get("capabilities", existing.capabilities),
            expires_at=payload.get("expires_at", existing.expires_at), max_tokens=payload.get("max_tokens", existing.max_tokens),
            max_calls=payload.get("max_calls", existing.max_calls), max_price_usd=payload.get("max_price_usd", existing.max_price_usd),
        )
        return self._public(result)

    def clear_secret(self, profile_id: str) -> dict[str, Any]:
        return self._public(self.store.clear_secret(profile_id))

    def revoke_profile(self, profile_id: str) -> dict[str, Any]:
        return self._public(self.store.revoke_profile(profile_id))

    def role_bindings(self) -> dict[str, str]:
        freeze = self.store.latest_freeze()
        if freeze:
            return dict(freeze.role_bindings)
        result: dict[str, str] = {}
        for row in self.store.list_profiles():
            for role in row.roles:
                result.setdefault(role, row.profile_id)
        return result

    def readiness(self, profile_id: str | None = None) -> dict[str, Any]:
        rows = self.store.list_profiles()
        selected = [row for row in rows if profile_id is None or row.profile_id == profile_id]
        now = datetime.now(timezone.utc)
        values = []
        for row in selected:
            expires = datetime.fromisoformat(row.expires_at) if row.expires_at else None
            from ops.ai_model_policy import policy_for
            try: policy = policy_for(self.store, row.profile_id)
            except Exception: policy = None
            values.append({"profile_id": row.profile_id, "ready": row.configured and not row.revoked and policy is not None and (expires is None or expires > now), "configured": row.configured, "revoked": row.revoked, "price_known": policy is not None, "version": row.version})
        return {"ready": bool(values) and all(item["ready"] for item in values), "profiles": values, "profile_id": profile_id}

    def snapshot_for_roles(self, bindings: Mapping[str, str]) -> ProfileSnapshot:
        clean = {str(k): str(v) for k, v in bindings.items()}
        if set(clean) != {role.value for role in Role}:
            raise ProfileValidationError("planner, executor, and verifier role bindings are required")
        freeze = self.store.freeze(clean)
        return profile_snapshot(self.store, freeze)


class SDKSidecar(Protocol):
    def status(self) -> Mapping[str, Any]: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def dispatch(self, request: Mapping[str, Any], *, heartbeat: Callable[[], None], cancelled: Callable[[], bool]) -> Mapping[str, Any]: ...


class SubprocessSDKSidecar:
    """Pinned isolated interpreter with an authenticated parent-owned pipe broker."""
    def __init__(self, python=None, *, source=None, entrypoint=None):
        self.python = Path(python) if python else None
        from ops.worker_isolation import config_from_environment
        self.isolation = config_from_environment()
        self.source_root = Path(source) if source else Path(__file__).resolve().parents[1]
        self.launcher = os.environ.get("D3M0N_AI_WORKER_LAUNCHER")
        if self.launcher:
            from gitd.services.fleet_sdk_broker import build_sdk_worker_command
            build_sdk_worker_command(self.python, self.launcher)
        self.source = Path(source) if source else None
        self._probe = None
        self._probe_at = 0.0
    def status(self):
        secure_mode = self.launcher in {"/usr/local/libexec/d3m0n-ai-worker", "/usr/local/libexec/d3m0n-ai-worker-client"} or self.isolation.mode in {"linux-host-isolation", "wsl2-host-isolation"}
        test_mode = os.environ.get("D3M0N_ALLOW_TEST_WORKER_FALLBACK") == "1"
        if not secure_mode and not test_mode:
            return {"ready": False, "activation_ready": False, "configured": False, "state": "not-ready", "reason": "secure Linux isolation or dedicated WSL2 host is required"}
        if not secure_mode and (not self.python or not self.python.exists()):
            return {"ready": False, "activation_ready": False, "configured": False, "state": "test-interpreter-missing"}
        if self._probe is None or time.monotonic() - self._probe_at > 60:
            from gitd.services.fleet_sdk_broker import build_sdk_child_environment
            env = build_sdk_child_environment(self.source_root)
            try:
                from gitd.services.fleet_sdk_broker import build_sdk_worker_command
                command = build_sdk_worker_command(self.python, self.launcher, probe=True, config=self.isolation)
                # The daemon's inherited /dev/null cannot be registered with epoll.
                # Empty input creates a real closed pipe for the opaque relay.
                result=subprocess.run(command,cwd=str(self.source_root),env=env,input="",capture_output=True,text=True,encoding="utf-8",timeout=45)
                self._probe=json.loads(result.stdout) if result.returncode==0 and len(result.stdout)<8192 else {"ready":False,"state":"probe-failed"}
            except Exception: self._probe={"ready":False,"state":"probe-failed"}
            self._probe_at=time.monotonic()
        return dict(self._probe, configured=True, activation_ready=bool(self._probe.get("ready")) and secure_mode,
                    protocol="mobilerun-pipe-v1", host_isolation="restricted-no-network" if secure_mode else "local-test-process")
    def capabilities(self):
        ready=bool(self.status().get("ready"))
        return [{"name":cap.value,"available":ready,"state":"ready" if ready else "blocked"} for cap in Capability]
    def start(self):
        status = self.status()
        if not status.get("activation_ready"):
            raise RuntimeError(status.get("reason", "pinned SDK isolation probe failed"))
    def stop(self):
        pass  # Each active child is fenced/cancelled by the controller and owns its process.
    def dispatch(self, request, *, heartbeat, cancelled, broker=None):
        if broker is None: raise RuntimeError("parent broker is mandatory")
        from gitd.services.fleet_sdk_broker import execute_process
        return execute_process(self.python,self.source_root,broker,mode=request["mode"],heartbeat=heartbeat,cancelled=cancelled,launcher=self.launcher,config=self.isolation)


@dataclass(frozen=True)
class FleetServiceConfig:
    state_dir: Path
    activation_enabled: bool = False
    sdk_python: Path | None = None
    sdk_source: Path | None = None
    lease_seconds: int = 30
    worker_id: str = "d3m0n-fleet"
    target_source: TargetInventory | None = None
    bridge: Any | None = None
    sidecar: SDKSidecar | None = None
    profiles: ProfileService | None = None


def default_service_config() -> FleetServiceConfig:
    state = Path(os.environ.get("D3M0N_AI_FLEET_STATE_DIR", str(Path.home() / ".local" / "state" / "d3m0n" / "ai-fleet")))
    sdk = os.environ.get("D3M0N_MOBILERUN_PYTHON", "").strip()
    source = os.environ.get("D3M0N_MOBILERUN_SDK_SOURCE", "").strip()
    return FleetServiceConfig(state_dir=state, activation_enabled=os.environ.get("D3M0N_AI_FLEET_ACTIVATION", "0").lower() in {"1", "true", "yes"}, sdk_python=Path(sdk) if sdk else None, sdk_source=Path(source) if source else None)


__all__ = ["FleetServiceConfig", "ImmutableTargetInventory", "ProfileStoreService", "SubprocessSDKSidecar", "TargetInventory", "SDKSidecar", "build_fleet_controller", "default_service_config"]


def build_fleet_controller(config: FleetServiceConfig | None = None):
    from ops.ai_fleet_controller import FleetController
    config = config or default_service_config()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    profiles = config.profiles or ProfileStoreService(ProfileStore(config.state_dir / "profiles"))
    sidecar = config.sidecar or SubprocessSDKSidecar(config.sdk_python, source=config.sdk_source)
    inventory = config.target_source or (ImmutableTargetInventory.from_env() if os.environ.get("D3M0N_AI_FLEET_TARGETS") else NativeTargetInventory())
    gateway = CloudGateway(profiles.store) if isinstance(profiles, ProfileStoreService) else None
    store = JobStore(config.state_dir / "jobs.sqlite3", lease_seconds=config.lease_seconds)
    bridge = config.bridge
    if bridge is None:
        from ops.ai_control_guard import AIControlGuard
        from gitd.services.native_bridge import NativeBridge
        bridge = NativeBridge(AIControlGuard(store), package_allowlist=frozenset({"com.android.settings", "com.sec.android.app.launcher", "com.google.android.apps.nexuslauncher", "com.android.launcher", "com.android.launcher3"}))
    return FleetController(store=store, profiles=profiles, runtime=sidecar, target_source=inventory, activation_enabled=config.activation_enabled, sidecar=sidecar, bridge=bridge, gateway=gateway, worker_id=config.worker_id)
