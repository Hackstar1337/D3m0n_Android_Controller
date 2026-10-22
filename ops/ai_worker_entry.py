"""Restricted worker entrypoint for one D3m0n Mobilerun child.

Host/service identity, bridge construction, and profile gateway construction are
injected by the parent application.  This module never reads credentials, uses
ADB/shell, or creates a provider connection itself.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from ai_contracts import JobState
from ops.ai_fleet_jobs import JobStore, LeaseError
from gitd.services.mobilerun_runtime import (
    MobilerunRuntime,
    RuntimeCancelled,
    RuntimeUnavailable,
)


@dataclass(frozen=True)
class WorkerResult:
    child_id: str | None
    state: JobState | None
    summary: str
    uncertain: bool = False


def _cancelled(cancel: Any) -> bool:
    if cancel is None:
        return False
    if callable(cancel):
        return bool(cancel())
    probe = getattr(cancel, "is_set", None)
    return bool(probe()) if callable(probe) else bool(cancel)


async def run_worker_once(
    store: JobStore,
    worker_id: str,
    bridge: Any,
    profiles: Any,
    cancel: Any,
    *,
    runtime: MobilerunRuntime | None = None,
) -> WorkerResult:
    """Claim at most one child and execute it under its current fencing token.

    ``bridge`` and ``profiles`` are deliberately required parameters.  There is
    no default adapter, no environment credential lookup, and no raw device
    fallback.  An uncertain SDK/provider return is never retried automatically.
    """
    if _cancelled(cancel):
        return WorkerResult(None, JobState.CANCELLED, "worker cancelled before claim")
    lease = store.claim_next(worker_id)
    if lease is None:
        return WorkerResult(None, None, "no queued child")
    job = store.get_job(lease.parent_job_id)
    if job is None:
        raise LeaseError("parent job disappeared")
    child = next((item for item in job.children if item.child_id == lease.child_id), None)
    if child is None:
        raise LeaseError("claimed child disappeared")
    # ChildSummary is immutable and intentionally does not expose the fence in
    # browser DTOs.  Keep the internal worker-only lease beside it.
    child_values = child.model_dump()
    # Some Pydantic revisions include the optional worker field in model_dump;
    # remove it before attaching the authoritative claimed lease identity.
    child_values.pop("fence", None)
    child_values.pop("lease_worker_id", None)
    child_values["target"] = child.target
    leased_child = SimpleNamespace(**child_values, fence=lease.fence, lease_worker_id=lease.worker_id)
    active_runtime = runtime or MobilerunRuntime()
    try:
        outcome = await active_runtime.run_child(job, leased_child, store, bridge, profiles, cancel)
        success = bool(outcome.get("success", False)) if isinstance(outcome, Mapping) else False
        state = JobState.SUCCEEDED if success else JobState.FAILED
        summary = str(outcome.get("reason", "completed"))[:1000] if isinstance(outcome, Mapping) else "completed"
        store.finish_child(lease.child_id, lease.worker_id, lease.fence, state, summary)
        return WorkerResult(lease.child_id, state, summary)
    except RuntimeCancelled:
        # Cancellation is safe only at a boundary; any in-flight operation is
        # not replayed and remains visible for owner reconciliation.
        try:
            store.finish_child(lease.child_id, lease.worker_id, lease.fence, JobState.UNCERTAIN, "cancelled during child execution")
        except Exception:
            store.reconcile_child(lease.child_id, JobState.UNCERTAIN, summary="cancelled during child execution")
        return WorkerResult(lease.child_id, JobState.UNCERTAIN, "cancelled during child execution", True)
    except (asyncio.TimeoutError, RuntimeUnavailable) as exc:
        # Readiness/timeout does not prove a phone mutation outcome.  Do not
        # automatically replay; the owner must reconcile before retrying.
        summary = str(exc)[:1000]
        try:
            store.finish_child(lease.child_id, lease.worker_id, lease.fence, JobState.UNCERTAIN, summary)
        except Exception:
            store.reconcile_child(lease.child_id, JobState.UNCERTAIN, summary=summary)
        return WorkerResult(lease.child_id, JobState.UNCERTAIN, summary, True)


__all__ = ["WorkerResult", "run_worker_once"]
