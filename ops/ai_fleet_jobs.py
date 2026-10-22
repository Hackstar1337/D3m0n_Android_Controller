"""Durable, provider-independent orchestration for the Mobilerun fleet.

This module is deliberately a control-plane store only. It never imports an SDK,
provider, ADB, shell, or native device adapter. Every adapter mutation must first
obtain an authorization for the current child fence and append a metadata-only
receipt after the native boundary returns.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import uuid
from typing import Any, Iterator

from ai_contracts import (
    Authorization, BudgetReservation, Capability, Checkpoint, ChildSummary,
    JobAction, JobDetail, JobLease, JobState, JobSummary, PlanCreatePayload,
    PlanCreateResult, PlanSummary, PolicySnapshot, ProfileSnapshot, StepOutcome,
    StepReceipt, TargetRef, TargetSnapshot, Intent, redact_metadata, utc_now,
)

from ops.platform_config import state_dir

_DEFAULT_STATE_DIR = state_dir() / "ai-fleet"
_STATE_ENV = "D3M0N_AI_FLEET_STATE_DIR"
_MICROS = Decimal("1000000")


class JobStoreError(RuntimeError):
    """Base class for safe control-plane errors."""


class NotFoundError(JobStoreError):
    pass


class IdempotencyError(JobStoreError):
    pass


class LeaseError(JobStoreError):
    pass


class ScopeError(JobStoreError):
    pass


class TransitionError(JobStoreError):
    pass


class BudgetError(JobStoreError):
    pass


class RedactionError(JobStoreError):
    pass


class ApprovalError(JobStoreError):
    pass


def _now_text() -> str:
    return utc_now().isoformat()


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(redact_metadata(value), sort_keys=True, separators=(",", ":"))


def _micros(value: Decimal | str | int | float) -> int:
    try:
        decimal = Decimal(str(value)).quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError) as exc:
        raise BudgetError("budget amount is invalid") from exc
    if decimal < 0:
        raise BudgetError("budget amount cannot be negative")
    return int(decimal * _MICROS)


def _decimal(value: int) -> Decimal:
    return (Decimal(value) / _MICROS).quantize(Decimal("0.000001"))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _uuid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class JobStore:
    """SQLite-backed plans/jobs with compare-and-set leases and fencing."""

    def __init__(self, db_path: str | Path | None = None, *, lease_seconds: int = 30):
        if lease_seconds < 5 or lease_seconds > 300:
            raise ValueError("lease_seconds must be between 5 and 300")
        if db_path is None:
            state_dir = Path(os.environ.get(_STATE_ENV, str(_DEFAULT_STATE_DIR))).expanduser()
            db_path = state_dir / "jobs.sqlite3"
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self._schema_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _initialize(self) -> None:
        with self._schema_lock, closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    intent_json TEXT NOT NULL,
                    target_snapshot_json TEXT NOT NULL,
                    profile_snapshot_json TEXT NOT NULL,
                    policy_snapshot_json TEXT NOT NULL,
                    approval_token_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    approval_expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    parent_id TEXT REFERENCES jobs(job_id) ON DELETE CASCADE,
                    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
                    target_id TEXT,
                    serial TEXT,
                    target_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    worker_id TEXT,
                    fence INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    latest_summary TEXT NOT NULL DEFAULT '',
                    latest_step INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(parent_id, target_id)
                );
                CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs(state, parent_id, lease_expires_at);
                CREATE INDEX IF NOT EXISTS jobs_parent_idx ON jobs(parent_id);
                CREATE TABLE IF NOT EXISTS approvals (
                    plan_id TEXT PRIMARY KEY REFERENCES plans(plan_id) ON DELETE CASCADE,
                    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    approved_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS authorizations (
                    authorization_id TEXT PRIMARY KEY,
                    child_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    serial TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    fence INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    UNIQUE(child_id, capability, fence)
                );
                CREATE TABLE IF NOT EXISTS step_receipts (
                    step_id TEXT PRIMARY KEY,
                    child_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    receipt_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(child_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    child_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    capability TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    child_id TEXT REFERENCES jobs(job_id) ON DELETE SET NULL,
                    reservation_key TEXT NOT NULL,
                    amount_micros INTEGER NOT NULL,
                    settled_micros INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    price_known INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, reservation_key)
                );
                """
            )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _ensure_policy(payload: PlanCreatePayload, targets: TargetSnapshot, policy: PolicySnapshot | None) -> PolicySnapshot:
        resolved = policy or payload.policy or PolicySnapshot()
        if len(targets.targets) > resolved.max_targets or len(targets.targets) > 100:
            raise ScopeError("target count exceeds the approved maximum")
        if resolved.max_active_workers > 3:
            raise ScopeError("active worker cap exceeds the foundation maximum")
        return resolved

    def create_plan(
        self,
        payload: PlanCreatePayload,
        target_snapshot: TargetSnapshot,
        profile_snapshot: ProfileSnapshot,
        policy_snapshot: PolicySnapshot | None = None,
    ) -> PlanCreateResult:
        policy = self._ensure_policy(payload, target_snapshot, policy_snapshot)
        plan_id = _uuid("plan")
        token = secrets.token_urlsafe(32)
        created = utc_now()
        expiry = policy.expiry
        with self._tx() as conn:
            if payload.idempotency_key:
                existing = conn.execute("SELECT plan_id FROM plans WHERE idempotency_key=?", (payload.idempotency_key,)).fetchone()
                if existing:
                    raise IdempotencyError("plan idempotency key already exists; use its plan ID")
            conn.execute(
                """INSERT INTO plans(plan_id,idempotency_key,intent_json,target_snapshot_json,profile_snapshot_json,
                   policy_snapshot_json,approval_token_hash,state,created_at,approval_expires_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id, payload.idempotency_key, _json(payload.intent.model_dump(mode="json")),
                    _json(target_snapshot.model_dump(mode="json")), _json(profile_snapshot.model_dump(mode="json")),
                    _json(policy.model_dump(mode="json")), _hash_token(token), JobState.QUEUED.value,
                    created.isoformat(), expiry.isoformat() if expiry else None,
                ),
            )
        return PlanCreateResult(
            plan=PlanSummary(plan_id=plan_id, state=JobState.QUEUED, target_count=len(target_snapshot.targets), created_at=created, approval_expires_at=expiry),
            approval_token=token,
        )

    def approve_plan(self, plan_id: str, token: str, idempotency_key: str) -> JobDetail:
        if not idempotency_key or len(idempotency_key) < 8:
            raise IdempotencyError("approval idempotency key is required")
        now = utc_now()
        with self._tx() as conn:
            plan = conn.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if not plan:
                raise NotFoundError("plan not found")
            prior = conn.execute("SELECT job_id FROM approvals WHERE plan_id=?", (plan_id,)).fetchone()
            if prior:
                return self._detail(conn, prior["job_id"])
            if plan["approval_expires_at"] and _dt(plan["approval_expires_at"]) <= now:
                raise ApprovalError("approval has expired")
            if not hmac.compare_digest(str(plan["approval_token_hash"]), _hash_token(token)):
                raise ApprovalError("approval token is invalid")
            intent = json.loads(plan["intent_json"])
            targets = json.loads(plan["target_snapshot_json"])
            profiles = json.loads(plan["profile_snapshot_json"])
            policy = json.loads(plan["policy_snapshot_json"])
            parent_id = _uuid("job")
            conn.execute(
                "INSERT INTO jobs(job_id,parent_id,plan_id,target_id,serial,target_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (parent_id, None, plan_id, None, None, _json(targets), JobState.QUEUED.value, now.isoformat(), now.isoformat()),
            )
            for target in targets["targets"]:
                child_id = _uuid("child")
                conn.execute(
                    "INSERT INTO jobs(job_id,parent_id,plan_id,target_id,serial,target_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (child_id, parent_id, plan_id, target["target_id"], target["serial"], _json(target), JobState.QUEUED.value, now.isoformat(), now.isoformat()),
                )
            conn.execute("INSERT INTO approvals(plan_id,job_id,idempotency_key,approved_at) VALUES(?,?,?,?)", (plan_id, parent_id, idempotency_key, now.isoformat()))
            conn.execute("UPDATE plans SET state=? WHERE plan_id=?", (JobState.QUEUED.value, plan_id))
            # Snapshots are parsed only after insertion; their stored bytes are immutable.
            return self._detail(conn, parent_id)

    def get_job(self, job_id: str) -> JobDetail | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT job_id,parent_id FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                return None
            return self._detail(conn, row["parent_id"] or row["job_id"])

    def get_plan_review(self, plan_id: str) -> dict[str, Any]:
        """Return the immutable, display-safe plan snapshots without approval material."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if not row:
                raise NotFoundError("plan not found")
            return {
                "plan_id": plan_id,
                "intent": Intent.model_validate(json.loads(row["intent_json"])),
                "targets": TargetSnapshot.model_validate(json.loads(row["target_snapshot_json"])),
                "profiles": ProfileSnapshot.model_validate(json.loads(row["profile_snapshot_json"])),
                "policy": PolicySnapshot.model_validate(json.loads(row["policy_snapshot_json"])),
            }

    def get_step_receipts(self, job_id: str, *, child_id: str | None = None, limit: int = 100) -> tuple[StepReceipt, ...]:
        """Read bounded, redacted receipts for a job; evidence blobs are never projected by the API."""
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        with closing(self._connect()) as conn:
            root = self._root_row(conn, job_id)
            args: list[Any] = [root["job_id"]]
            query = "SELECT receipt_json FROM step_receipts WHERE child_id IN (SELECT job_id FROM jobs WHERE parent_id=?)"
            if child_id:
                child = conn.execute("SELECT job_id FROM jobs WHERE job_id=? AND parent_id=?", (child_id, root["job_id"])).fetchone()
                if not child:
                    raise ScopeError("child is outside the requested job")
                query = "SELECT receipt_json FROM step_receipts WHERE child_id=?"
                args = [child_id]
            query += " ORDER BY created_at ASC, sequence ASC LIMIT ?"
            args.append(limit)
            return tuple(StepReceipt.model_validate(json.loads(row["receipt_json"])) for row in conn.execute(query, args).fetchall())

    def list_jobs(self, *, state: JobState | None = None, limit: int = 100) -> tuple[JobSummary, ...]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        with closing(self._connect()) as conn:
            query = "SELECT job_id FROM jobs WHERE parent_id IS NULL"
            args: list[Any] = []
            if state is not None:
                query += " AND state=?"
                args.append(state.value)
            query += " ORDER BY created_at DESC LIMIT ?"
            args.append(limit)
            rows = conn.execute(query, args).fetchall()
            return tuple(self._summary(conn, row["job_id"]) for row in rows)

    def claim_next(self, worker_id: str) -> JobLease | None:
        worker_id = worker_id.strip()
        if not worker_id or len(worker_id) > 120:
            raise LeaseError("worker ID is invalid")
        now = utc_now()
        expires = now + timedelta(seconds=self.lease_seconds)
        with self._tx() as conn:
            # A lost running lease is ambiguous, never silently replay it.
            expired = conn.execute("SELECT job_id,parent_id FROM jobs WHERE parent_id IS NOT NULL AND state=? AND lease_expires_at IS NOT NULL AND lease_expires_at<=?", (JobState.RUNNING.value, now.isoformat())).fetchall()
            for row in expired:
                conn.execute("UPDATE jobs SET state=?,worker_id=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=? AND state=?", (JobState.MANUAL_RECOVERY.value, now.isoformat(), row["job_id"], JobState.RUNNING.value))
                conn.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=? AND state IN (?,?)", (JobState.MANUAL_RECOVERY.value, now.isoformat(), row["parent_id"], JobState.QUEUED.value, JobState.RUNNING.value))
            candidate = conn.execute(
                """SELECT child.*, parent.state AS parent_state, parent.target_json AS parent_targets,
                          parent.plan_id AS parent_plan_id, json_extract(parent.target_json,'$.targets') AS unused
                   FROM jobs child JOIN jobs parent ON parent.job_id=child.parent_id
                   WHERE child.parent_id IS NOT NULL AND child.state IN (?,?)
                     AND parent.state IN (?,?,?,?,?)
                     AND NOT EXISTS (SELECT 1 FROM jobs busy WHERE busy.parent_id IS NOT NULL
                                     AND busy.serial=child.serial AND busy.state=?
                                     AND busy.lease_expires_at>?)
                   ORDER BY child.created_at ASC LIMIT 1""",
                (JobState.QUEUED.value, JobState.RETRY.value, JobState.QUEUED.value, JobState.RUNNING.value, JobState.WAITING_FOR_OWNER.value, JobState.UNCERTAIN.value, JobState.RETRY.value, JobState.RUNNING.value, now.isoformat()),
            ).fetchone()
            if not candidate:
                return None
            policy = self._policy_for_job(conn, candidate["parent_id"])
            active = conn.execute("SELECT COUNT(*) FROM jobs WHERE parent_id=? AND state=? AND lease_expires_at>?", (candidate["parent_id"], JobState.RUNNING.value, now.isoformat())).fetchone()[0]
            if active >= policy.max_active_workers:
                return None
            next_fence = int(candidate["fence"]) + 1
            conn.execute(
                "UPDATE jobs SET state=?,worker_id=?,fence=?,lease_expires_at=?,updated_at=? WHERE job_id=? AND state IN (?,?)",
                (JobState.RUNNING.value, worker_id, next_fence, expires.isoformat(), now.isoformat(), candidate["job_id"], JobState.QUEUED.value, JobState.RETRY.value),
            )
            if candidate["parent_state"] in (JobState.QUEUED.value, JobState.WAITING_FOR_OWNER.value, JobState.UNCERTAIN.value, JobState.RETRY.value):
                conn.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=? AND state=?", (JobState.RUNNING.value, now.isoformat(), candidate["parent_id"], candidate["parent_state"]))
            return JobLease(child_id=candidate["job_id"], parent_job_id=candidate["parent_id"], target_id=candidate["target_id"], serial=candidate["serial"], worker_id=worker_id, fence=next_fence, lease_expires_at=expires)

    def heartbeat(self, child_id: str, worker_id: str, fence: int) -> JobLease:
        now = utc_now()
        expires = now + timedelta(seconds=self.lease_seconds)
        with self._tx() as conn:
            row = self._lease_row(conn, child_id, worker_id, fence, now)
            conn.execute("UPDATE jobs SET lease_expires_at=?,updated_at=? WHERE job_id=?", (expires.isoformat(), now.isoformat(), child_id))
            return JobLease(child_id=child_id, parent_job_id=row["parent_id"], target_id=row["target_id"], serial=row["serial"], worker_id=worker_id, fence=fence, lease_expires_at=expires)

    def authorize(self, child_id: str, serial: str, capability: Capability, fence: int) -> Authorization:
        now = utc_now()
        with self._tx() as conn:
            try:
                row = self._lease_row(conn, child_id, None, fence, now)
            except (NotFoundError, LeaseError) as exc:
                raise ScopeError("child is not currently authorized") from exc
            if row["serial"] != serial:
                raise ScopeError("serial does not match the immutable target snapshot")
            policy = self._policy_for_job(conn, row["parent_id"])
            if capability not in policy.capabilities:
                raise ScopeError("capability is outside the immutable policy snapshot")
            if policy.scope.value != "admin" and capability.value in {"group_write", "proxy_apply", "proxy_prepare", "proxy_disable", "file_write", "apk_install", "reboot", "usb_recovery"}:
                raise ScopeError("administrative capability requires admin scope")
            expiry = min(_dt(row["lease_expires_at"]), policy.expiry or (now + timedelta(seconds=self.lease_seconds)))
            authorization_id = _uuid("auth")
            conn.execute("INSERT OR REPLACE INTO authorizations(authorization_id,child_id,serial,capability,fence,expires_at) VALUES(?,?,?,?,?,?)", (authorization_id, child_id, serial, capability.value, fence, expiry.isoformat()))
            return Authorization(child_id=child_id, serial=serial, capability=capability, fence=fence, expires_at=expiry)

    def append_step(self, child_id: str, worker_id: str, fence: int, receipt: StepReceipt) -> StepReceipt:
        if receipt.child_id != child_id:
            raise RedactionError("receipt child does not match lease")
        now = utc_now()
        with self._tx() as conn:
            self._lease_row(conn, child_id, worker_id, fence, now)
            auth = conn.execute("SELECT 1 FROM authorizations WHERE child_id=? AND capability=? AND fence=? AND expires_at>?", (child_id, receipt.capability.value, fence, now.isoformat())).fetchone()
            if not auth:
                raise ScopeError("capability was not authorized for this fence")
            existing = conn.execute("SELECT receipt_json FROM step_receipts WHERE step_id=?", (receipt.step_id,)).fetchone()
            if existing:
                stored = StepReceipt.model_validate(json.loads(existing["receipt_json"]))
                if stored.child_id == child_id and stored.sequence == receipt.sequence:
                    return stored
                raise IdempotencyError("step ID is already used")
            latest = conn.execute("SELECT latest_step FROM jobs WHERE job_id=?", (child_id,)).fetchone()[0]
            if receipt.sequence != int(latest) + 1:
                raise TransitionError("step sequence must be the next sequence")
            safe = receipt.model_copy(update={"evidence": redact_metadata(receipt.evidence), "created_at": receipt.created_at})
            conn.execute("INSERT INTO step_receipts(step_id,child_id,sequence,receipt_json,created_at) VALUES(?,?,?,?,?)", (receipt.step_id, child_id, receipt.sequence, _json(safe.model_dump(mode="json")), now.isoformat()))
            conn.execute("UPDATE jobs SET latest_step=?,latest_summary=?,updated_at=? WHERE job_id=?", (receipt.sequence, safe.summary, now.isoformat(), child_id))
            if receipt.outcome in {StepOutcome.UNCERTAIN, StepOutcome.WAITING_FOR_OWNER}:
                status = JobState.UNCERTAIN if receipt.outcome == StepOutcome.UNCERTAIN else JobState.WAITING_FOR_OWNER
                conn.execute("UPDATE jobs SET state=?,worker_id=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?", (status.value, now.isoformat(), child_id))
                conn.execute("INSERT INTO checkpoints(checkpoint_id,child_id,status,reason,capability,created_at) VALUES(?,?,?,?,?,?)", (_uuid("checkpoint"), child_id, status.value, safe.summary or status.value, receipt.capability.value, now.isoformat()))
                self._refresh_parent(conn, self._parent_id(conn, child_id), now)
            return safe

    def finish_child(self, child_id: str, worker_id: str, fence: int, status: JobState, summary: str = "") -> JobDetail:
        allowed = {JobState.SUCCEEDED, JobState.FAILED, JobState.WAITING_FOR_OWNER, JobState.UNCERTAIN, JobState.MANUAL_RECOVERY, JobState.STOPPED}
        if status not in allowed:
            raise TransitionError("finish status is not a terminal or owner-checkpoint state")
        now = utc_now()
        with self._tx() as conn:
            row = self._lease_row(conn, child_id, worker_id, fence, now)
            conn.execute("UPDATE jobs SET state=?,worker_id=NULL,fence=?,lease_expires_at=NULL,latest_summary=?,updated_at=? WHERE job_id=?", (status.value, fence + 1, redact_metadata(summary), now.isoformat(), child_id))
            parent = self._parent_id(conn, child_id)
            self._refresh_parent(conn, parent, now)
            return self._detail(conn, parent)

    def control_job(self, job_id: str, action: JobAction) -> JobDetail:
        now = utc_now()
        with self._tx() as conn:
            root = self._root_row(conn, job_id)
            parent_id = root["job_id"]
            child_rows = conn.execute("SELECT * FROM jobs WHERE parent_id=?", (parent_id,)).fetchall()
            if action == JobAction.PAUSE:
                if root["state"] in {JobState.STOPPED.value, JobState.CANCELLED.value, JobState.SUCCEEDED.value}:
                    raise TransitionError("job is already terminal")
                # A pause is a fence, not a request for a running adapter to finish
                # later. Running children lose their lease/fence and require owner
                # confirmation, so resume can never replay an uncertain mutation.
                running = [row for row in child_rows if row["state"] == JobState.RUNNING.value]
                for child in running:
                    conn.execute("UPDATE jobs SET state=?,worker_id=NULL,fence=fence+1,lease_expires_at=NULL,updated_at=? WHERE job_id=? AND state=?", (JobState.WAITING_FOR_OWNER.value, now.isoformat(), child["job_id"], JobState.RUNNING.value))
                    conn.execute("DELETE FROM authorizations WHERE child_id=?", (child["job_id"],))
                    conn.execute("INSERT INTO checkpoints(checkpoint_id,child_id,status,reason,capability,created_at) VALUES(?,?,?,?,?,?)", (_uuid("checkpoint"), child["job_id"], JobState.WAITING_FOR_OWNER.value, "Pause fenced the active child; owner must confirm the safe checkpoint before resume", None, now.isoformat()))
                conn.execute("UPDATE jobs SET state=?,updated_at=? WHERE parent_id=? AND state IN (?,?)", (JobState.PAUSED.value, now.isoformat(), parent_id, JobState.QUEUED.value, JobState.RETRY.value))
                # Keep the parent explicitly paused even when a child checkpoint
                # would otherwise cause _refresh_parent to project waiting_for_owner.
                conn.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=?", (JobState.PAUSED.value, now.isoformat(), parent_id))
            elif action == JobAction.RESUME:
                if root["state"] not in {JobState.PAUSED.value, JobState.WAITING_FOR_OWNER.value, JobState.RETRY.value}:
                    raise TransitionError("job cannot be resumed from its current state")
                blocked = conn.execute("SELECT 1 FROM jobs WHERE parent_id=? AND state IN (?,?,?) LIMIT 1", (parent_id, JobState.WAITING_FOR_OWNER.value, JobState.UNCERTAIN.value, JobState.MANUAL_RECOVERY.value)).fetchone()
                if blocked:
                    conn.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=?", (JobState.WAITING_FOR_OWNER.value, now.isoformat(), parent_id))
                else:
                    conn.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=?", (JobState.RUNNING.value, now.isoformat(), parent_id))
                    conn.execute("UPDATE jobs SET state=?,updated_at=? WHERE parent_id=? AND state=?", (JobState.QUEUED.value, now.isoformat(), parent_id, JobState.PAUSED.value))
            elif action == JobAction.STOP:
                conn.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=?", (JobState.STOPPING.value, now.isoformat(), parent_id))
                conn.execute("UPDATE jobs SET state=?,worker_id=NULL,fence=fence+1,lease_expires_at=NULL,updated_at=? WHERE parent_id=? AND state IN (?,?,?,?,?)", (JobState.STOPPED.value, now.isoformat(), parent_id, JobState.QUEUED.value, JobState.RETRY.value, JobState.PAUSED.value, JobState.RUNNING.value, JobState.WAITING_FOR_OWNER.value))
                self._refresh_parent(conn, parent_id, now, force_stop=True)
            elif action == JobAction.RETRY:
                conn.execute("UPDATE jobs SET state=?,worker_id=NULL,lease_expires_at=NULL,updated_at=? WHERE parent_id=? AND state=?", (JobState.RETRY.value, now.isoformat(), parent_id, JobState.FAILED.value))
                conn.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=? AND state NOT IN (?,?,?,?)", (JobState.RETRY.value, now.isoformat(), parent_id, JobState.STOPPED.value, JobState.CANCELLED.value, JobState.SUCCEEDED.value, JobState.UNCERTAIN.value))
            else:
                raise TransitionError("unsupported job action")
            return self._detail(conn, parent_id)

    def reconcile_child(self, child_id: str, outcome: JobState, *, summary: str = "") -> JobDetail:
        allowed = {JobState.SUCCEEDED, JobState.FAILED, JobState.RETRY, JobState.STOPPED, JobState.MANUAL_RECOVERY, JobState.UNCERTAIN}
        if outcome not in allowed:
            raise TransitionError("reconciliation outcome is not allowed")
        now = utc_now()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=? AND parent_id IS NOT NULL", (child_id,)).fetchone()
            if not row:
                raise NotFoundError("child job not found")
            if row["state"] not in {JobState.UNCERTAIN.value, JobState.MANUAL_RECOVERY.value, JobState.WAITING_FOR_OWNER.value}:
                raise TransitionError("child does not require reconciliation")
            conn.execute("UPDATE jobs SET state=?,worker_id=NULL,fence=fence+1,lease_expires_at=NULL,latest_summary=?,updated_at=? WHERE job_id=?", (outcome.value, redact_metadata(summary), now.isoformat(), child_id))
            conn.execute("UPDATE checkpoints SET status=? WHERE child_id=? AND status=?", (outcome.value, child_id, JobState.WAITING_FOR_OWNER.value))
            parent = row["parent_id"]
            self._refresh_parent(conn, parent, now)
            return self._detail(conn, parent)

    def reserve_budget(self, job_id: str, amount_usd: Decimal, reservation_key: str, *, price_known: bool = True, child_id: str | None = None) -> BudgetReservation:
        if not price_known:
            raise BudgetError("unknown pricing prevents cloud activation")
        amount = _micros(amount_usd)
        if amount <= 0 or not reservation_key or len(reservation_key) > 160:
            raise BudgetError("reservation must have a positive amount and key")
        now = utc_now()
        with self._tx() as conn:
            root = self._root_row(conn, job_id)
            existing = conn.execute("SELECT * FROM reservations WHERE job_id=? AND reservation_key=?", (root["job_id"], reservation_key)).fetchone()
            if existing:
                return self._reservation(existing)
            policy = self._policy_for_job(conn, root["job_id"])
            cap = _micros(policy.budget_usd)
            reserved = conn.execute("SELECT COALESCE(SUM(amount_micros),0) FROM reservations WHERE job_id=? AND state!='released'", (root["job_id"],)).fetchone()[0]
            if int(reserved) + amount > cap:
                raise BudgetError("budget reservation exceeds immutable job ceiling")
            if child_id:
                child = conn.execute("SELECT 1 FROM jobs WHERE job_id=? AND parent_id=?", (child_id, root["job_id"])).fetchone()
                if not child:
                    raise ScopeError("reservation child is outside the job")
            reservation_id = _uuid("budget")
            conn.execute("INSERT INTO reservations(reservation_id,job_id,child_id,reservation_key,amount_micros,state,price_known,created_at) VALUES(?,?,?,?,?,?,?,?)", (reservation_id, root["job_id"], child_id, reservation_key, amount, "reserved", 1, now.isoformat()))
            return self._reservation(conn.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone())

    def settle_budget(self, reservation_id: str, *, actual_amount_usd: Decimal | None = None, usage_known: bool = True) -> BudgetReservation:
        if not usage_known:
            raise BudgetError("unknown usage prevents exact settlement")
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if not row:
                raise NotFoundError("budget reservation not found")
            if row["state"] == "settled":
                return self._reservation(row)
            actual = int(row["amount_micros"] if actual_amount_usd is None else _micros(actual_amount_usd))
            if actual > int(row["amount_micros"]):
                raise BudgetError("settlement exceeds reservation")
            conn.execute("UPDATE reservations SET state='settled',settled_micros=? WHERE reservation_id=?", (actual, reservation_id))
            return self._reservation(conn.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone())

    # ---- internal projection and transition helpers ----
    def _root_row(self, conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise NotFoundError("job not found")
        if row["parent_id"]:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (row["parent_id"],)).fetchone()
        return row

    def _parent_id(self, conn: sqlite3.Connection, child_id: str) -> str:
        row = conn.execute("SELECT parent_id FROM jobs WHERE job_id=?", (child_id,)).fetchone()
        if not row or not row["parent_id"]:
            raise NotFoundError("child job not found")
        return row["parent_id"]

    def _lease_row(self, conn: sqlite3.Connection, child_id: str, worker_id: str | None, fence: int, now: datetime) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=? AND parent_id IS NOT NULL", (child_id,)).fetchone()
        if not row:
            raise NotFoundError("child job not found")
        if row["state"] != JobState.RUNNING.value or int(row["fence"]) != fence or (worker_id is not None and row["worker_id"] != worker_id):
            raise LeaseError("lease or fencing token is stale")
        if not row["lease_expires_at"] or _dt(row["lease_expires_at"]) <= now:
            raise LeaseError("lease has expired")
        return row

    def _policy_for_job(self, conn: sqlite3.Connection, parent_id: str) -> PolicySnapshot:
        row = conn.execute("SELECT plan_id FROM jobs WHERE job_id=?", (parent_id,)).fetchone()
        if not row:
            raise NotFoundError("parent job not found")
        plan = conn.execute("SELECT policy_snapshot_json FROM plans WHERE plan_id=?", (row["plan_id"],)).fetchone()
        return PolicySnapshot.model_validate(json.loads(plan["policy_snapshot_json"]))

    def _refresh_parent(self, conn: sqlite3.Connection, parent_id: str, now: datetime, *, force_stop: bool = False) -> None:
        children = conn.execute("SELECT state FROM jobs WHERE parent_id=?", (parent_id,)).fetchall()
        states = [JobState(row["state"]) for row in children]
        if force_stop and not any(state in {JobState.UNCERTAIN, JobState.MANUAL_RECOVERY} for state in states):
            state = JobState.STOPPED
        elif any(state == JobState.MANUAL_RECOVERY for state in states):
            state = JobState.MANUAL_RECOVERY
        elif any(state == JobState.UNCERTAIN for state in states):
            state = JobState.UNCERTAIN
        elif any(state == JobState.WAITING_FOR_OWNER for state in states):
            state = JobState.WAITING_FOR_OWNER
        elif all(state in {JobState.SUCCEEDED} for state in states):
            state = JobState.SUCCEEDED
        elif all(state.terminal for state in states):
            state = JobState.PARTIALLY_SUCCEEDED if any(state == JobState.SUCCEEDED for state in states) else JobState.FAILED
        elif any(state == JobState.RUNNING for state in states):
            state = JobState.RUNNING
        elif any(state in {JobState.RETRY, JobState.QUEUED} for state in states):
            state = JobState.RETRY if any(state == JobState.RETRY for state in states) else JobState.QUEUED
        else:
            state = JobState.PAUSED
        conn.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=?", (state.value, now.isoformat(), parent_id))

    def _summary(self, conn: sqlite3.Connection, job_id: str) -> JobSummary:
        row = self._root_row(conn, job_id)
        children = conn.execute("SELECT state FROM jobs WHERE parent_id=?", (row["job_id"],)).fetchall()
        reserved = conn.execute("SELECT COALESCE(SUM(amount_micros),0) FROM reservations WHERE job_id=? AND state!='released'", (row["job_id"],)).fetchone()[0]
        settled = conn.execute("SELECT COALESCE(SUM(settled_micros),0) FROM reservations WHERE job_id=?", (row["job_id"],)).fetchone()[0]
        states = [JobState(x["state"]) for x in children]
        return JobSummary(job_id=row["job_id"], plan_id=row["plan_id"], state=JobState(row["state"]), target_count=len(states), completed_count=sum(x in {JobState.SUCCEEDED, JobState.FAILED, JobState.STOPPED} for x in states), uncertain_count=sum(x in {JobState.UNCERTAIN, JobState.MANUAL_RECOVERY} for x in states), budget_reserved_usd=_decimal(int(reserved)), budget_settled_usd=_decimal(int(settled)), created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]))

    def _detail(self, conn: sqlite3.Connection, job_id: str) -> JobDetail:
        row = self._root_row(conn, job_id)
        plan = conn.execute("SELECT * FROM plans WHERE plan_id=?", (row["plan_id"],)).fetchone()
        summary = self._summary(conn, row["job_id"])
        intent = Intent.model_validate(json.loads(plan["intent_json"]))
        targets = TargetSnapshot.model_validate(json.loads(plan["target_snapshot_json"]))
        profiles = ProfileSnapshot.model_validate(json.loads(plan["profile_snapshot_json"]))
        policy = PolicySnapshot.model_validate(json.loads(plan["policy_snapshot_json"]))
        children: list[ChildSummary] = []
        target_by_id = {target.target_id: target for target in targets.targets}
        for child in conn.execute("SELECT * FROM jobs WHERE parent_id=? ORDER BY created_at", (row["job_id"],)).fetchall():
            target = target_by_id.get(child["target_id"], TargetRef(target_id=child["target_id"], serial=child["serial"], online=False))
            children.append(ChildSummary(child_id=child["job_id"], target=target, state=JobState(child["state"]), latest_summary=child["latest_summary"], latest_step=child["latest_step"], lease_worker_id=child["worker_id"], uncertain=child["state"] in {JobState.UNCERTAIN.value, JobState.MANUAL_RECOVERY.value}))
        checkpoints = tuple(Checkpoint(checkpoint_id=cp["checkpoint_id"], child_id=cp["child_id"], status=JobState(cp["status"]), reason=cp["reason"], capability=Capability(cp["capability"]) if cp["capability"] else None, created_at=_dt(cp["created_at"])) for cp in conn.execute("SELECT * FROM checkpoints WHERE child_id IN (SELECT job_id FROM jobs WHERE parent_id=?) ORDER BY created_at", (row["job_id"],)).fetchall())
        return JobDetail(**summary.model_dump(), intent=intent, target_snapshot=targets, profile_snapshot=profiles, policy_snapshot=policy, children=tuple(children), checkpoints=checkpoints)

    @staticmethod
    def _reservation(row: sqlite3.Row) -> BudgetReservation:
        return BudgetReservation(reservation_id=row["reservation_id"], job_id=row["job_id"], child_id=row["child_id"], reservation_key=row["reservation_key"], amount_usd=_decimal(int(row["amount_micros"])), settled_usd=_decimal(int(row["settled_micros"])), state=row["state"], price_known=bool(row["price_known"]), created_at=_dt(row["created_at"]))


__all__ = [
    "ApprovalError", "BudgetError", "IdempotencyError", "JobStore", "JobStoreError",
    "LeaseError", "NotFoundError", "RedactionError", "ScopeError", "TransitionError",
]
