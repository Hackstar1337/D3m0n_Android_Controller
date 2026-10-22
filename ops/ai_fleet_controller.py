"""Application controller for the local-first Mobilerun fleet API.

The controller is intentionally an adapter seam: it owns API orchestration and
never imports Mobilerun, a provider client, ADB, or a native device writer.
Those services are injected by the later runtime/bridge builders.  The default
seams are fail-closed and report unavailable capabilities rather than claiming
that a cloud runtime is active.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any, Mapping, Protocol

def _worker_error_summary(exc: Exception) -> str:
    """Expose only bounded exception class names, never exception payloads."""
    tag = type(exc).__name__
    if not tag.isidentifier() or len(tag) > 80:
        tag = "WorkerError"
    if tag == "WorkerProtocolError":
        text = str(exc)
        prefix = "SDK failed: "
        if text.startswith(prefix):
            parts = text[len(prefix):].split("/")
            if 1 <= len(parts) <= 5 and all(p.isidentifier() and len(p) < 80 for p in parts):
                tag += "/" + "/".join(parts)
    safe_files = {"native_bridge.py", "fleet_sdk_broker.py", "sdk_worker_protocol.py", "ai_fleet_jobs.py", "control_arbiter.py",
                  "ai_control_guard.py", "adb.py", "device_context.py", "native_observation.py", "fast_control.py"}
    chain, node, depth = [], exc, 0
    while node is not None and depth < 4:
        sites, tb = [], node.__traceback__
        while tb is not None:
            name = Path(tb.tb_frame.f_code.co_filename).name
            if name in safe_files:
                sites.append(name + ":" + str(tb.tb_lineno))
            tb = tb.tb_next
        label = type(node).__name__ if type(node).__name__.isidentifier() else "Error"
        chain.append(label + ("@" + sites[-1] if sites else ""))
        node, depth = node.__cause__ or node.__context__, depth + 1
    if chain:
        tag = tag.split("@")[0] if "/" in tag else ""
        tag = (tag + " " if tag else "") + "<-".join(chain)
    return ("Worker interrupted (" + tag + "); native state requires review")[:240]

from ai_contracts import (
    Capability,
    Checkpoint,
    JobAction,
    JobDetail,
    JobState,
    JobSummary,
    PlanCreatePayload,
    PlanCreateResult,
    PolicySnapshot,
    ProfileRef,
    ProfileSnapshot,
    Role,
    TargetRef,
    TargetSnapshot,
)
from ops.ai_fleet_jobs import (
    ApprovalError,
    BudgetError,
    IdempotencyError,
    JobStore,
    JobStoreError,
    LeaseError,
    NotFoundError,
    ScopeError,
    TransitionError,
)


class FleetServiceUnavailable(RuntimeError):
    """A required optional service is not configured in this process."""


class FleetProfileService(Protocol):
    """Metadata-only profile service implemented by the encrypted profile owner."""

    def list_profiles(self) -> list[dict[str, Any]]: ...

    def get_profile(self, profile_id: str) -> dict[str, Any] | None: ...

    def create_profile(self, data: Mapping[str, Any]) -> dict[str, Any]: ...

    def update_profile(self, profile_id: str, data: Mapping[str, Any]) -> dict[str, Any]: ...

    def delete_profile(self, profile_id: str) -> None: ...

    def role_bindings(self) -> dict[str, str]: ...

    def readiness(self, profile_id: str | None = None) -> dict[str, Any]: ...


class FleetRuntimeService(Protocol):
    """Read-only status/capability view of the separately pinned SDK runtime."""

    def status(self) -> Mapping[str, Any]: ...

    def capabilities(self) -> list[Mapping[str, Any]]: ...


class UnconfiguredProfileService:
    def _unavailable(self) -> None:
        raise FleetServiceUnavailable("fleet profiles are not configured")

    def list_profiles(self) -> list[dict[str, Any]]:
        self._unavailable(); return []

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        self._unavailable(); return None

    def create_profile(self, data: Mapping[str, Any]) -> dict[str, Any]:
        self._unavailable(); return {}

    def update_profile(self, profile_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        self._unavailable(); return {}

    def delete_profile(self, profile_id: str) -> None:
        self._unavailable()

    def role_bindings(self) -> dict[str, str]:
        self._unavailable(); return {}

    def readiness(self, profile_id: str | None = None) -> dict[str, Any]:
        self._unavailable(); return {}


class UnconfiguredRuntimeService:
    def status(self) -> Mapping[str, Any]:
        return {
            "state": "not-installed",
            "activation": "disabled",
            "configured": False,
            "ready": False,
            "reason": "Mobilerun runtime is not registered in this process",
        }

    def capabilities(self) -> list[Mapping[str, Any]]:
        return []


@dataclass(frozen=True)
class FleetTargetSource:
    """Optional inventory seam; it must return safe TargetRef metadata only."""

    list_targets: Any


_DEFAULT_CAPABILITIES: tuple[dict[str, Any], ...] = tuple(
    {
        "name": capability.value,
        "available": False,
        "state": "blocked",
        "reason": "native bridge/runtime adapter is not registered",
        "mutating": capability.value in {
            "input", "navigate", "app_state", "group_write", "proxy_apply",
            "proxy_prepare", "proxy_disable", "file_write", "apk_install",
            "reboot", "usb_recovery",
        },
    }
    for capability in Capability
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_metadata(value: Any) -> Any:
    """Limit event/status data to JSON-safe, display metadata."""
    if isinstance(value, Mapping):
        return {str(key): _safe_metadata(item) for key, item in value.items() if str(key).lower() not in {"token", "api_key", "secret", "password", "credential", "prompt", "screenshot", "tree", "response"}}
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(item) for item in value[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) else value[:1000]
    return str(value)[:1000]


class FleetController:
    """Owner-authoritative API facade over the additive JobStore."""

    def __init__(
        self,
        *,
        store: JobStore | None = None,
        store_factory: Any | None = None,
        profiles: FleetProfileService | None = None,
        runtime: FleetRuntimeService | None = None,
        target_source: FleetTargetSource | None = None,
        activation_enabled: bool | None = None,
        sidecar: Any | None = None,
        bridge: Any | None = None,
        gateway: Any | None = None,
        worker_id: str = "d3m0n-fleet",
    ) -> None:
        self._store = store
        self._store_factory = store_factory or JobStore
        self.profiles = profiles or UnconfiguredProfileService()
        self.runtime = runtime or UnconfiguredRuntimeService()
        self.target_source = target_source
        self.activation_enabled = bool(activation_enabled) if activation_enabled is not None else False
        self.sidecar = sidecar
        self.bridge = bridge
        self.gateway = gateway
        self.worker_id = worker_id
        self._worker_thread: threading.Thread | None = None
        self._worker_stop = threading.Event()
        self._worker_threads = []

    @property
    def store(self) -> JobStore:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    def status(self) -> dict[str, Any]:
        try:
            runtime = _safe_metadata(dict(self.runtime.status()))
        except Exception:
            runtime = {"state": "unavailable", "configured": False, "ready": False}
        profile_ready = False
        try:
            profile_ready = bool(self.profiles.readiness().get("ready"))
        except Exception:
            pass
        runtime.setdefault("activation", "disabled")
        runtime["profile_ready"] = profile_ready
        if not self.activation_enabled:
            runtime["activation"] = "disabled"
            runtime["ready"] = False
            runtime["reason"] = "cloud activation is disabled by default"
        elif not runtime.get("ready") or not profile_ready:
            runtime["activation"] = "enabled-but-not-ready"
            runtime["ready"] = False
            runtime["reason"] = "SDK and active encrypted profiles are required"
        return {
            "ok": True,
            "activation": "enabled" if self.activation_enabled and runtime.get("ready") else "disabled",
            "runtime": runtime,
            "worker_cap": 3,
            "budget_ceiling_usd": "20.000000",
            "control_plane": "configured",
            "worker": "running" if self._worker_thread and self._worker_thread.is_alive() else "stopped",
            "timestamp": _utc(),
        }

    def activation(self) -> dict[str, Any]:
        return {"enabled": self.activation_enabled, "status": self.status()}

    def set_activation(self, enabled: bool) -> dict[str, Any]:
        if enabled:
            runtime_ready = bool(self.runtime.status().get("ready"))
            profile_ready = bool(self.profiles.readiness().get("ready"))
            if not runtime_ready or not profile_ready:
                raise FleetServiceUnavailable("SDK and active encrypted profiles are not ready")
            self.activation_enabled = True
            self.start_worker()
        else:
            self.activation_enabled = False
            self.stop_worker()
        return self.activation()

    def start_worker(self) -> None:
        if not self.activation_enabled or self.sidecar is None:
            return
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._worker_stop.clear()
        try:
            self.sidecar.start()
        except Exception:
            return
        self._worker_threads = [threading.Thread(target=self._worker_loop, args=(f"{self.worker_id}-{i}",), name=f"d3m0n-ai-fleet-{i}", daemon=True) for i in range(3)]
        self._worker_thread = self._worker_threads[0]
        for thread in self._worker_threads: thread.start()

    def stop_worker(self) -> None:
        self._worker_stop.set()
        for thread in self._worker_threads:
            if thread.is_alive() and thread is not threading.current_thread(): thread.join(timeout=3)
        self._worker_threads = []
        self._worker_thread = None
        if self.sidecar is not None:
            try: self.sidecar.stop()
            except Exception: pass

    def _worker_loop(self, worker_id) -> None:
        while not self._worker_stop.wait(0.1):
            if not self.activation_enabled:
                continue
            try:
                self.dispatch_once(worker_id)
            except Exception:
                self._worker_stop.wait(0.5)

    def dispatch_once(self, worker_id=None):
        """One fenced child with concrete SDK/native/provider callbacks."""
        if not self.activation_enabled or self.sidecar is None: return None
        lease=self.store.claim_next(worker_id or self.worker_id)
        if lease is None: return None
        detail=self.get_job(lease.parent_job_id)
        child=next(c for c in detail.children if c.child_id==lease.child_id)
        broker=None
        def cancelled():
            if self._worker_stop.is_set() or not self.activation_enabled: return True
            try: self.store.authorize(lease.child_id,lease.serial,Capability.OBSERVE,lease.fence); return False
            except Exception: return True
        def heartbeat(): self.store.heartbeat(lease.child_id,lease.worker_id,lease.fence)
        try:
            from gitd.services.fleet_sdk_broker import ChildBroker
            broker=ChildBroker(self,detail,child,lease,cancelled)
            mode=detail.intent.metadata.get("runtime_mode",detail.intent.metadata.get("mode","direct"))
            if mode not in {"direct","reasoning"}: raise ScopeError("invalid frozen SDK mode")
            result=self.sidecar.dispatch({"mode":mode},heartbeat=heartbeat,cancelled=cancelled,broker=broker)
            status=JobState(result.get("state","uncertain"))
            summary=str(result.get("summary","Native evidence requires review"))[:240]
            if status!=JobState.WAITING_FOR_OWNER:
                self.store.finish_child(lease.child_id,lease.worker_id,lease.fence,status,summary)
            return {"child_id":lease.child_id,"state":status.value,"summary":summary}
        except Exception as exc:
            current=self.get_job(detail.job_id)
            current_child=next(c for c in current.children if c.child_id==lease.child_id)
            if current_child.state==JobState.RUNNING:
                try: self.store.finish_child(lease.child_id,lease.worker_id,lease.fence,JobState.UNCERTAIN,_worker_error_summary(exc))
                except Exception: pass
            return {"child_id":lease.child_id,"state":current_child.state.value if current_child.state!=JobState.RUNNING else "uncertain","summary":"Worker stopped or requires owner review"}
        finally:
            if broker is not None: broker.close()

    def capabilities(self) -> list[dict[str, Any]]:
        try:
            provided = self.runtime.capabilities()
        except Exception:
            provided = []
        by_name = {str(item.get("name")): dict(item) for item in provided if isinstance(item, Mapping)}
        result = []
        for base in _DEFAULT_CAPABILITIES:
            item = dict(base)
            if item["name"] in by_name:
                item.update(_safe_metadata(by_name[item["name"]]))
            if not self.activation_enabled:
                item["available"] = False
                item["state"] = "disabled"
            result.append(item)
        return result

    def create_plan(
        self,
        payload: PlanCreatePayload,
        targets: TargetSnapshot,
        profiles: ProfileSnapshot,
        policy: PolicySnapshot | None = None,
    ) -> PlanCreateResult:
        now = datetime.now(timezone.utc)
        # The request's configured/profile/target booleans are display-only. Build
        # immutable snapshots from server-owned services and reject drift.
        if self.target_source is not None:
            actual = self.target_source.list_targets() if hasattr(self.target_source, "list_targets") else self.target_source.snapshot().targets
            actual_by_id = {item.target_id: item for item in actual}
            if any(item.target_id not in actual_by_id or actual_by_id[item.target_id].serial != item.serial for item in targets.targets):
                raise ScopeError("target selection does not match current inventory")
            targets = TargetSnapshot(targets=tuple(actual_by_id[item.target_id] for item in targets.targets), version=targets.version)
        actual_rows = {str(row.get("profile_id")): row for row in self.profiles.list_profiles()}
        for profile in profiles.profiles:
            actual = actual_rows.get(profile.profile_id)
            if not actual or not actual.get("configured") or actual.get("revoked"):
                raise ScopeError("profile is not configured")
            if not profile.configured:
                raise ScopeError("profile is not configured")
            if actual.get("provider") != profile.provider or actual.get("model") != profile.model or actual.get("endpoint_origin") != profile.endpoint_origin:
                raise ScopeError("profile metadata does not match encrypted storage")
            if profile.expires_at is not None:
                expiry = profile.expires_at if profile.expires_at.tzinfo else profile.expires_at.replace(tzinfo=timezone.utc)
                if expiry <= now:
                    raise ScopeError("profile has expired")
        try:
            bindings = {key.value if isinstance(key, Role) else str(key): value for key, value in profiles.role_bindings.items()}
            if set(bindings) != {role.value for role in Role}:
                raise ScopeError("all owner role bindings are required")
            if set(bindings.values()) - set(actual_rows):
                raise ScopeError("role binding references unknown profile")
        except AttributeError:
            raise ScopeError("profile snapshot is invalid") from None
        versions = {pid: int(actual_rows[pid].get("version", 1)) for pid in set(bindings.values())}
        refs = tuple(ProfileRef(profile_id=pid, provider=actual_rows[pid]["provider"], model=actual_rows[pid]["model"], endpoint_origin=actual_rows[pid]["endpoint_origin"], configured=True, expires_at=actual_rows[pid].get("expires_at"), capabilities=frozenset(actual_rows[pid].get("capabilities", ()))) for pid in sorted(versions))
        for role, pid in bindings.items():
            if role not in actual_rows[pid].get("roles", ()): raise ScopeError("profile is not configured for its selected role")
        profiles = ProfileSnapshot(profiles=refs, role_bindings={Role(k):v for k,v in bindings.items()}, profile_versions=versions)
        return self.store.create_plan(payload, targets, profiles, policy)

    def approve(self, plan_id: str, token: str, idempotency_key: str) -> JobDetail:
        return self.store.approve_plan(plan_id, token, idempotency_key)

    def list_jobs(self, *, state: JobState | None = None, limit: int = 100) -> tuple[JobSummary, ...]:
        return self.store.list_jobs(state=state, limit=limit)

    def get_job(self, job_id: str) -> JobDetail:
        detail = self.store.get_job(job_id)
        if detail is None:
            raise NotFoundError("job not found")
        return detail

    def action(self, job_id: str, action: JobAction) -> JobDetail:
        if action not in {JobAction.PAUSE, JobAction.RESUME, JobAction.STOP, JobAction.RETRY}:
            raise TransitionError("unsupported job action")
        detail = self.store.control_job(job_id, action)
        if action in {JobAction.STOP, JobAction.PAUSE} and self.bridge is not None:
            for child in detail.children:
                self.bridge.guard.stop(child.child_id)
        return detail

    def checkpoints(self, job_id: str) -> tuple[Checkpoint, ...]:
        return self.get_job(job_id).checkpoints

    def reconcile(self, job_id: str, child_id: str, outcome: JobState, summary: str = "",
                  grant_capability: str | None = None, grant_operation: str | None = None) -> JobDetail:
        detail = self.get_job(job_id)
        if child_id not in {child.child_id for child in detail.children}:
            raise ScopeError("child is outside the requested job")
        if grant_capability is not None:
            from ops.ai_owner_grants import get_owner_grants
            allowed = {cap.value for cap in detail.policy_snapshot.capabilities}
            if outcome != JobState.RETRY or str(grant_capability) not in allowed:
                raise ScopeError("owner grant is outside this job's approved capabilities")
            child = next(item for item in detail.children if item.child_id == child_id)
            if child.state not in {JobState.WAITING_FOR_OWNER, JobState.UNCERTAIN, JobState.MANUAL_RECOVERY}:
                raise ScopeError("owner grant requires a phone that is waiting for owner review")
            get_owner_grants().issue(child_id, str(grant_capability), str(grant_operation or ""))
        return self.store.reconcile_child(child_id, outcome, summary=summary)

    def events(self, job_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        detail = self.get_job(job_id)
        events: list[dict[str, Any]] = [{
            "type": "job",
            "job_id": detail.job_id,
            "state": detail.state.value,
            "created_at": detail.created_at.isoformat(),
            "updated_at": detail.updated_at.isoformat(),
        }]
        for child in detail.children:
            events.append({
                "type": "child",
                "child_id": child.child_id,
                "target_id": child.target.target_id,
                "serial": child.target.serial,
                "state": child.state.value,
                "latest_step": child.latest_step,
                "summary": child.latest_summary,
            })
        for checkpoint in detail.checkpoints:
            events.append({
                "type": "checkpoint",
                "checkpoint_id": checkpoint.checkpoint_id,
                "child_id": checkpoint.child_id,
                "state": checkpoint.status.value,
                "reason": checkpoint.reason,
                "capability": checkpoint.capability.value if checkpoint.capability else None,
                "created_at": checkpoint.created_at.isoformat(),
            })
        return [_safe_metadata(item) for item in events[-limit:]]

    def list_profiles(self) -> list[dict[str, Any]]:
        return [_safe_metadata(item) for item in self.profiles.list_profiles()]

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        item = self.profiles.get_profile(profile_id)
        return _safe_metadata(item) if item is not None else None

    def create_profile(self, data: Mapping[str, Any]) -> dict[str, Any]:
        return _safe_metadata(self.profiles.create_profile(data))

    def update_profile(self, profile_id: str, data: Mapping[str, Any], *, secret: str | None = None) -> dict[str, Any]:
        updater = getattr(self.profiles, "update_profile")
        return _safe_metadata(updater(profile_id, data, secret=secret) if secret is not None else updater(profile_id, data))

    def save_profile(self, data: Mapping[str, Any], *, secret: str | None = None) -> dict[str, Any]:
        saver = getattr(self.profiles, "save_profile", None) or getattr(self.profiles, "create_profile")
        return _safe_metadata(saver(data, secret=secret) if secret is not None else saver(data))

    def clear_profile_secret(self, profile_id: str) -> dict[str, Any]:
        return _safe_metadata(self.profiles.clear_secret(profile_id))

    def revoke_profile(self, profile_id: str) -> dict[str, Any]:
        return _safe_metadata(self.profiles.revoke_profile(profile_id))

    def delete_profile(self, profile_id: str) -> None:
        self.profiles.delete_profile(profile_id)

    def profile_roles(self) -> dict[str, str]:
        return {str(key): str(value) for key, value in self.profiles.role_bindings().items() if str(key) in {role.value for role in Role}}

    def profile_readiness(self, profile_id: str | None = None) -> dict[str, Any]:
        return _safe_metadata(self.profiles.readiness(profile_id))

    def target_snapshot(self, targets: list[TargetRef] | None = None) -> TargetSnapshot:
        if targets:
            return TargetSnapshot(targets=tuple(targets))
        if self.target_source is None:
            raise ScopeError("explicit target snapshot is required until inventory is configured")
        resolved = self.target_source.list_targets()
        return TargetSnapshot(targets=tuple(resolved))

    @staticmethod
    def map_error(exc: Exception) -> tuple[int, str]:
        if isinstance(exc, (NotFoundError,)):
            return 404, str(exc)
        if isinstance(exc, (ApprovalError,)):
            return 403, str(exc)
        if isinstance(exc, (IdempotencyError,)):
            return 409, str(exc)
        if isinstance(exc, (LeaseError, ScopeError, TransitionError, BudgetError)):
            return 409, str(exc)
        if isinstance(exc, FleetServiceUnavailable):
            return 503, str(exc)
        if isinstance(exc, ValueError):
            return 400, str(exc)
        if isinstance(exc, JobStoreError):
            return 409, str(exc)
        return 503, "fleet service unavailable"


_default_controller: FleetController | None = None


def get_controller() -> FleetController:
    """Lazy singleton built from the concrete service factory."""
    global _default_controller
    if _default_controller is None:
        from ops.ai_fleet_services import build_fleet_controller
        _default_controller = build_fleet_controller()
    return _default_controller


def set_controller(controller: FleetController | None) -> None:
    """Test/runtime injection seam; production startup may replace this once configured."""
    global _default_controller
    _default_controller = controller


__all__ = [
    "FleetController", "FleetProfileService", "FleetRuntimeService", "FleetServiceUnavailable",
    "FleetTargetSource", "get_controller", "set_controller",
]
