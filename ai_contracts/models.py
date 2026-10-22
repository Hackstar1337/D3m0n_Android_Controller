"""Closed, metadata-only contracts for the Mobilerun fleet foundation."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SECRET_KEY = re.compile(r"(?:api[_-]?key|secret|password|passwd|credential|authorization|bearer|token|cookie|screenshot|screen|tree|prompt|provider[_-]?response|raw[_-]?response)", re.I)
_SECRET_VALUE = re.compile(r"(?i)(?:sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._~+/=-]{8,}|(?:api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+)")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_text(value: str, *, max_length: int, field: str = "value") -> str:
    value = value.strip()
    if len(value) > max_length:
        raise ValueError(f"{field} exceeds maximum length")
    if _SECRET_VALUE.search(value):
        raise ValueError(f"{field} contains credential-like material")
    return value


def redact_metadata(value: Any, *, _key: str = "") -> Any:
    """Return safe JSON-like metadata; never retain sensitive keys or raw blobs."""
    if _key and _SECRET_KEY.search(_key):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {str(k): redact_metadata(v, _key=str(k)) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_metadata(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[redacted]", value)[:2000]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:2000]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_OWNER = "waiting_for_owner"
    RETRY = "retry"
    UNCERTAIN = "uncertain"
    MANUAL_RECOVERY = "manual_recovery"
    STOPPING = "stopping"
    STOPPED = "stopped"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.STOPPED, self.SUCCEEDED, self.FAILED, self.PARTIALLY_SUCCEEDED, self.CANCELLED}


class Role(str, Enum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    VERIFIER = "verifier"


class Capability(str, Enum):
    INVENTORY = "inventory"
    OBSERVE = "observe"
    NAVIGATE = "navigate"
    INPUT = "input"
    APP_STATE = "app_state"
    GROUP_READ = "group_read"
    PROXY_READ = "proxy_read"
    FILE_READ = "file_read"
    REBOOT_PREFLIGHT = "reboot_preflight"
    RECOVERY_STATUS = "recovery_status"
    SYNC_STATUS = "sync_status"
    STREAM_STATUS = "stream_status"
    GROUP_WRITE = "group_write"
    PROXY_TEST = "proxy_test"
    PROXY_APPLY = "proxy_apply"
    PROXY_PREPARE = "proxy_prepare"
    PROXY_DISABLE = "proxy_disable"
    FILE_WRITE = "file_write"
    APK_INSTALL = "apk_install"
    REBOOT = "reboot"
    USB_RECOVERY = "usb_recovery"


class ScopeKind(str, Enum):
    NAVIGATION = "navigation"
    INPUT = "input"
    APP_STATE = "app_state"
    ADMIN = "admin"


class JobAction(str, Enum):
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    RETRY = "retry"


class StepOutcome(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNCERTAIN = "uncertain"
    WAITING_FOR_OWNER = "waiting_for_owner"


class TargetRef(ClosedModel):
    target_id: str = Field(min_length=1, max_length=100)
    serial: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=160)
    online: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("target_id", "serial", "label")
    @classmethod
    def safe_text(cls, value: str, info: Any) -> str:
        return _clean_text(value, max_length=200, field=info.field_name)


class TargetSnapshot(ClosedModel):
    targets: tuple[TargetRef, ...] = Field(min_length=1, max_length=100)
    version: str = Field(default="1", min_length=1, max_length=30)

    @model_validator(mode="after")
    def unique_targets(self) -> "TargetSnapshot":
        ids = [item.target_id for item in self.targets]
        serials = [item.serial for item in self.targets]
        if len(ids) != len(set(ids)) or len(serials) != len(set(serials)):
            raise ValueError("target IDs and serials must be unique")
        return self


class Intent(ClosedModel):
    text: str = Field(min_length=1, max_length=4000)
    request_id: str | None = Field(default=None, max_length=120)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def safe_intent(cls, value: str) -> str:
        return _clean_text(value, max_length=4000, field="intent")


class PlanCreatePayload(ClosedModel):
    intent: Intent
    policy: "PolicySnapshot | None" = None
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


class ProfileRef(ClosedModel):
    profile_id: str = Field(min_length=1, max_length=120)
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    endpoint_origin: str = Field(min_length=1, max_length=200)
    configured: bool = False
    expires_at: datetime | None = None
    capabilities: frozenset[str] = Field(default_factory=frozenset)


class ProfileSnapshot(ClosedModel):
    profiles: tuple[ProfileRef, ...] = Field(min_length=1, max_length=10)
    role_bindings: dict[Role, str]
    version: str = Field(default="1", min_length=1, max_length=30)
    profile_versions: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bindings_exist(self) -> "ProfileSnapshot":
        ids = {item.profile_id for item in self.profiles}
        if set(self.role_bindings) != set(Role):
            raise ValueError("planner, executor, and verifier role bindings are required")
        if not set(self.role_bindings.values()) <= ids:
            raise ValueError("role binding references unknown profile")
        return self


class PolicySnapshot(ClosedModel):
    scope: ScopeKind = ScopeKind.NAVIGATION
    capabilities: frozenset[Capability] = Field(default_factory=lambda: frozenset({Capability.OBSERVE, Capability.NAVIGATE, Capability.APP_STATE}))
    max_targets: int = Field(default=100, ge=1, le=100)
    max_active_workers: int = Field(default=3, ge=1, le=3)
    budget_usd: Decimal = Field(default=Decimal("20"), ge=Decimal("0"), le=Decimal("20"))
    allow_vision: bool = False
    expiry: datetime | None = None
    version: str = Field(default="1", min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_scope(self) -> "PolicySnapshot":
        if self.scope != ScopeKind.ADMIN and any(cap in self.capabilities for cap in {Capability.PROXY_APPLY, Capability.PROXY_DISABLE, Capability.FILE_WRITE, Capability.APK_INSTALL, Capability.REBOOT, Capability.USB_RECOVERY, Capability.GROUP_WRITE}):
            raise ValueError("administrative capabilities require admin scope")
        if self.expiry is not None and self.expiry <= utc_now():
            raise ValueError("policy expiry must be in the future")
        return self


class PlanSummary(ClosedModel):
    plan_id: str
    state: JobState
    target_count: int
    created_at: datetime
    approval_expires_at: datetime | None = None


class PlanCreateResult(ClosedModel):
    plan: PlanSummary
    approval_token: str = Field(min_length=20, max_length=256)


class StepReceipt(ClosedModel):
    step_id: str
    child_id: str
    sequence: int = Field(ge=1)
    capability: Capability
    outcome: StepOutcome
    summary: str = Field(default="", max_length=1000)
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("summary")
    @classmethod
    def safe_summary(cls, value: str) -> str:
        return _clean_text(value, max_length=1000, field="summary")

    @field_validator("evidence")
    @classmethod
    def safe_evidence(cls, value: dict[str, Any]) -> dict[str, Any]:
        return redact_metadata(value)


class JobLease(ClosedModel):
    child_id: str
    parent_job_id: str
    target_id: str
    serial: str
    worker_id: str
    fence: int = Field(ge=1)
    lease_expires_at: datetime


class Authorization(ClosedModel):
    child_id: str
    serial: str
    capability: Capability
    fence: int = Field(ge=1)
    expires_at: datetime


class BudgetReservation(ClosedModel):
    reservation_id: str
    job_id: str
    child_id: str | None = None
    reservation_key: str
    amount_usd: Decimal = Field(ge=Decimal("0"))
    settled_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    state: str = Field(pattern=r"^(reserved|settled|released)$")
    price_known: bool
    created_at: datetime


class Checkpoint(ClosedModel):
    checkpoint_id: str
    child_id: str
    status: JobState
    reason: str = Field(max_length=1000)
    capability: Capability | None = None
    created_at: datetime


class ChildSummary(ClosedModel):
    child_id: str
    target: TargetRef
    state: JobState
    latest_summary: str = ""
    latest_step: int = 0
    lease_worker_id: str | None = None
    uncertain: bool = False


class JobSummary(ClosedModel):
    job_id: str
    plan_id: str
    state: JobState
    target_count: int
    completed_count: int
    uncertain_count: int
    budget_reserved_usd: Decimal
    budget_settled_usd: Decimal
    created_at: datetime
    updated_at: datetime


class JobDetail(JobSummary):
    intent: Intent
    target_snapshot: TargetSnapshot
    profile_snapshot: ProfileSnapshot
    policy_snapshot: PolicySnapshot
    children: tuple[ChildSummary, ...] = ()
    checkpoints: tuple[Checkpoint, ...] = ()


PlanCreatePayload.model_rebuild()
