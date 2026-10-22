"""Authenticated, metadata-only HTTP API for the Mobilerun fleet control plane."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.responses import JSONResponse

from ai_contracts import (
    Capability,
    Intent,
    JobAction,
    JobState,
    PolicySnapshot,
    ProfileRef,
    Role,
    ScopeKind,
    TargetRef,
)
from ops.ai_fleet_controller import FleetServiceUnavailable, get_controller
from ops.platform_config import get_allowed_origins


NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
SAME_ORIGINS = get_allowed_origins()
MAX_BODY_BYTES = 128 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreatePlanBody(StrictModel):
    intent: Intent
    targets: tuple[TargetRef, ...] = Field(min_length=1, max_length=100)
    profiles: tuple[ProfileRef, ...] = Field(min_length=1, max_length=10)
    role_bindings: dict[Role, str]
    policy: PolicySnapshot | None = None
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)
    target_version: str = Field(default="1", min_length=1, max_length=30)
    profile_version: str = Field(default="1", min_length=1, max_length=30)

    @model_validator(mode="after")
    def bindings_exist(self) -> "CreatePlanBody":
        ids = {item.profile_id for item in self.profiles}
        if set(self.role_bindings) != set(Role) or not set(self.role_bindings.values()) <= ids:
            raise ValueError("planner, executor, and verifier bindings must reference supplied profiles")
        return self


class ApproveBody(StrictModel):
    approval_token: str = Field(min_length=20, max_length=256)
    idempotency_key: str = Field(min_length=8, max_length=160)


class ActionBody(StrictModel):
    action: JobAction


class ActivationBody(StrictModel):
    enabled: bool


class ReconcileBody(StrictModel):
    child_id: str = Field(min_length=1, max_length=120)
    outcome: JobState
    summary: str = Field(default="", max_length=1000)
    grant_capability: str | None = Field(default=None, max_length=40)
    grant_operation: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def safe_outcome(self) -> "ReconcileBody":
        if self.outcome not in {JobState.SUCCEEDED, JobState.FAILED, JobState.RETRY, JobState.STOPPED, JobState.MANUAL_RECOVERY, JobState.UNCERTAIN}:
            raise ValueError("outcome requires an owner reconciliation state")
        if (self.grant_capability is None) != (self.grant_operation is None):
            raise ValueError("grant capability and operation must be supplied together")
        if self.grant_capability is not None and self.outcome != JobState.RETRY:
            raise ValueError("an owner grant is only valid with a retry reconciliation")
        return self


class ProfileBody(StrictModel):
    profile_id: str = Field(min_length=1, max_length=120)
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    endpoint_origin: str = Field(min_length=1, max_length=200)
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    expires_at: datetime | None = None
    roles: frozenset[Role] = Field(default_factory=frozenset)
    # Accepted only on the authenticated write request. It is never projected
    # into a response, browser state, logs, or the durable job store.
    secret: str | None = Field(default=None, min_length=1, max_length=8192, repr=False)


class ProfilePatch(StrictModel):
    provider: str | None = Field(default=None, min_length=1, max_length=80)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    endpoint_origin: str | None = Field(default=None, min_length=1, max_length=200)
    capabilities: frozenset[str] | None = None
    expires_at: datetime | None = None
    roles: frozenset[Role] | None = None
    secret: str | None = Field(default=None, min_length=1, max_length=8192, repr=False)
    clear_secret: bool = False
    revoke: bool = False


router = APIRouter(prefix="/api/ai-fleet", tags=["ai-fleet"])


def _access(request: Request, *, write: bool = False) -> None:
    """Require the dashboard gate plus same-origin semantics for mutations."""
    from ops.dashboard_auth import is_trusted_local, session_cookie, state

    local = is_trusted_local(request.scope)
    authenticated = request.scope.get("d3m0n_auth") in {"local", "session"}
    if not authenticated and not local:
        config = state.config()
        token = session_cookie(request.scope)
        authenticated = bool(config and token and config.token_valid(token))
    if not authenticated:
        raise HTTPException(status_code=401, detail="Dashboard session required", headers=NO_STORE)
    # Fleet metadata is owner-only too: do not let a cross-site page read job or profile data.
    origin = request.headers.get("origin", "").strip()
    fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if origin and origin not in get_allowed_origins():
        raise HTTPException(status_code=403, detail="Same-origin request required", headers=NO_STORE)
    if fetch_site == "cross-site":
        raise HTTPException(status_code=403, detail="Same-origin request required", headers=NO_STORE)


def _json(data: Any, status_code: int = 200) -> JSONResponse:
    def encode(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return encode(value.model_dump(mode="json"))
        if isinstance(value, dict):
            return {str(key): encode(item) for key, item in value.items()}
        if isinstance(value, (tuple, list, set, frozenset)):
            return [encode(item) for item in value]
        return value

    return JSONResponse(encode(data), status_code=status_code, headers=NO_STORE)


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        status, detail = get_controller().map_error(exc)
        raise HTTPException(status_code=status, detail=detail, headers=NO_STORE) from None


@router.get("/status")
def fleet_status(request: Request) -> JSONResponse:
    _access(request)
    return _json(get_controller().status())


@router.get("/activation")
def fleet_activation(request: Request) -> JSONResponse:
    _access(request)
    return _json(_call(get_controller().activation))


@router.post("/activation")
def set_fleet_activation(request: Request, body: ActivationBody = Body(...)) -> JSONResponse:
    _access(request, write=True)
    return _json(_call(get_controller().set_activation, body.enabled))


@router.get("/capabilities")
def fleet_capabilities(request: Request) -> JSONResponse:
    _access(request)
    return _json({"capabilities": get_controller().capabilities(), "activation": get_controller().status()["activation"]})


@router.post("/plans", status_code=201)
def create_plan(request: Request, body: CreatePlanBody = Body(...)) -> JSONResponse:
    _access(request, write=True)
    from ai_contracts import PlanCreatePayload, ProfileSnapshot, TargetSnapshot

    payload = PlanCreatePayload(intent=body.intent, policy=body.policy, idempotency_key=body.idempotency_key)
    targets = TargetSnapshot(targets=body.targets, version=body.target_version)
    profiles = ProfileSnapshot(profiles=body.profiles, role_bindings=body.role_bindings, version=body.profile_version)
    result = _call(get_controller().create_plan, payload, targets, profiles, body.policy)
    review = _call(get_controller().store.get_plan_review, result.plan.plan_id)
    # The approval token is returned only on this one creation response; it is never
    # included in plans/jobs/history or any event projection.
    return _json({"plan": result.plan, "approval_token": result.approval_token, "review": review}, 201)


@router.post("/plans/{plan_id}/approve")
def approve_plan(plan_id: str, request: Request, body: ApproveBody = Body(...)) -> JSONResponse:
    _access(request, write=True)
    detail = _call(get_controller().approve, plan_id, body.approval_token, body.idempotency_key)
    return _json(detail)


@router.get("/jobs")
def list_jobs(
    request: Request,
    state: JobState | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
) -> JSONResponse:
    _access(request)
    return _json({"jobs": _call(get_controller().list_jobs, state=state, limit=limit)})


@router.get("/jobs/list")
def list_jobs_alias(
    request: Request,
    state: JobState | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
) -> JSONResponse:
    _access(request)
    return _json({"jobs": _call(get_controller().list_jobs, state=state, limit=limit)})


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> JSONResponse:
    _access(request)
    return _json(_call(get_controller().get_job, job_id))


@router.get("/jobs/{job_id}/receipts")
def job_receipts(
    job_id: str,
    request: Request,
    child_id: str | None = Query(default=None, min_length=1, max_length=120),
    limit: int = Query(default=100, ge=1, le=100),
) -> JSONResponse:
    """Return only safe receipt metadata; evidence/raw adapter data is never projected."""
    _access(request)
    controller = get_controller()
    detail = _call(controller.get_job, job_id)
    receipts = _call(controller.store.get_step_receipts, detail.job_id, child_id=child_id, limit=limit)
    projected = [
        {
            "step_id": receipt.step_id,
            "child_id": receipt.child_id,
            "sequence": receipt.sequence,
            "capability": receipt.capability.value,
            "outcome": receipt.outcome.value,
            "summary": receipt.summary,
            "created_at": receipt.created_at.isoformat(),
        }
        for receipt in receipts
    ]
    return _json({"receipts": projected, "bounded": True})


@router.get("/jobs/{job_id}/detail")
def get_job_detail_alias(job_id: str, request: Request) -> JSONResponse:
    _access(request)
    return _json(_call(get_controller().get_job, job_id))


@router.post("/jobs/{job_id}/actions")
def job_action(job_id: str, request: Request, body: ActionBody = Body(...)) -> JSONResponse:
    _access(request, write=True)
    return _json(_call(get_controller().action, job_id, body.action))


@router.get("/jobs/{job_id}/checkpoints")
def job_checkpoints(job_id: str, request: Request) -> JSONResponse:
    _access(request)
    return _json({"checkpoints": _call(get_controller().checkpoints, job_id)})


@router.post("/jobs/{job_id}/reconcile")
def job_reconcile(job_id: str, request: Request, body: ReconcileBody = Body(...)) -> JSONResponse:
    _access(request, write=True)
    return _json(_call(get_controller().reconcile, job_id, body.child_id, body.outcome, body.summary,
                       body.grant_capability, body.grant_operation))


@router.get("/jobs/{job_id}/events")
def job_events(
    job_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=100),
) -> JSONResponse:
    _access(request)
    return _json({"events": _call(get_controller().events, job_id, limit=limit), "bounded": True})


@router.get("/profiles")
def list_profiles(request: Request) -> JSONResponse:
    _access(request)
    return _json({"profiles": _call(get_controller().list_profiles)})


@router.get("/profiles/roles")
def profile_roles(request: Request) -> JSONResponse:
    _access(request)
    return _json({"roles": _call(get_controller().profile_roles)})


@router.get("/profiles/readiness")
def profile_readiness_all(request: Request) -> JSONResponse:
    _access(request)
    return _json(_call(get_controller().profile_readiness))


@router.get("/profiles/{profile_id}/readiness")
def profile_readiness(profile_id: str, request: Request) -> JSONResponse:
    _access(request)
    return _json(_call(get_controller().profile_readiness, profile_id))


@router.post("/profiles/{profile_id}/validate")
async def validate_profile_model(profile_id: str, request: Request) -> JSONResponse:
    _access(request, write=True)
    from ops.ai_model_policy import policy_for, refresh_openrouter_policy
    controller = get_controller()
    if controller.gateway is None:
        raise HTTPException(status_code=409, detail="Provider gateway is not configured", headers=NO_STORE)
    try:
        store = controller.gateway.profiles
        runtime = store.get_runtime(profile_id)
        if runtime["provider"] == "openrouter":
            await refresh_openrouter_policy(store, profile_id, transport=controller.gateway.transport)
        policy = policy_for(store, profile_id)
        if policy is None:
            raise ValueError("Model metadata is not registered")
    except Exception:
        raise HTTPException(status_code=409, detail="Verified pricing and compatible model metadata are required; no inference was performed", headers=NO_STORE) from None
    return _json({"model_policy_ready": True, "provider_access_tested": False,
                  "readiness": controller.profile_readiness(profile_id)})


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: str, request: Request) -> JSONResponse:
    _access(request)
    result = _call(get_controller().get_profile, profile_id)
    if result is None:
        raise HTTPException(status_code=404, detail="profile not found", headers=NO_STORE)
    return _json(result)


@router.post("/profiles", status_code=201)
def create_profile(request: Request, body: ProfileBody = Body(...)) -> JSONResponse:
    _access(request, write=True)
    values = body.model_dump(mode="json")
    secret = values.pop("secret", None)
    return _json(_call(get_controller().save_profile, values, secret=secret), 201)


@router.put("/profiles/{profile_id}")
def update_profile(profile_id: str, request: Request, body: ProfilePatch = Body(...)) -> JSONResponse:
    _access(request, write=True)
    values = body.model_dump(mode="json", exclude_unset=True)
    secret = values.pop("secret", None)
    clear_secret = bool(values.pop("clear_secret", False))
    revoke = bool(values.pop("revoke", False))
    if clear_secret:
        return _json(_call(get_controller().clear_profile_secret, profile_id))
    if revoke:
        return _json(_call(get_controller().revoke_profile, profile_id))
    return _json(_call(get_controller().update_profile, profile_id, values, secret=secret))


@router.patch("/profiles/{profile_id}")
def patch_profile(profile_id: str, request: Request, body: ProfilePatch = Body(...)) -> JSONResponse:
    return update_profile(profile_id, request, body)


@router.delete("/profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: str, request: Request) -> JSONResponse:
    _access(request, write=True)
    _call(get_controller().delete_profile, profile_id)
    return JSONResponse(None, status_code=204, headers=NO_STORE)


class FleetBodyLimit:
    """Bound JSON bodies before FastAPI/Pydantic parses them."""

    class _TooLarge(Exception):
        pass

    def __init__(self, app: Any, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith("/api/ai-fleet"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        try:
            length = int(headers.get(b"content-length", b"0"))
        except (TypeError, ValueError):
            length = self.limit + 1
        if length < 0 or length > self.limit:
            return await JSONResponse({"detail": "Fleet request body is too large"}, status_code=413, headers=NO_STORE)(scope, receive, send)
        seen = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal seen
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    raise FleetBodyLimit._TooLarge
            return message

        try:
            return await self.app(scope, limited_receive, send)
        except FleetBodyLimit._TooLarge:
            return await JSONResponse({"detail": "Fleet request body is too large"}, status_code=413, headers=NO_STORE)(scope, receive, send)


def install_ai_fleet(app: Any) -> None:
    """Register routes before dashboard auth; auth is installed later and stays outermost."""
    app.include_router(router)
    app.add_middleware(FleetBodyLimit)
    from fastapi.exceptions import RequestValidationError
    from fastapi.exception_handlers import request_validation_exception_handler
    async def safe_validation(request, exc):
        if request.url.path.startswith("/api/ai-fleet"):
            return JSONResponse({"detail":"Invalid fleet request"},status_code=422,headers=NO_STORE)
        return await request_validation_exception_handler(request,exc)
    app.add_exception_handler(RequestValidationError,safe_validation)
    async def fleet_startup():
        import asyncio
        controller=get_controller()
        controller.event_loop=asyncio.get_running_loop()
        controller.start_worker()
    async def fleet_shutdown():
        get_controller().stop_worker()
    from contextlib import asynccontextmanager
    original_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def fleet_lifespan(application):
        async with original_lifespan(application) as state:
            await fleet_startup()
            try:
                yield state
            finally:
                await fleet_shutdown()
    app.router.lifespan_context = fleet_lifespan


__all__ = ["FleetBodyLimit", "install_ai_fleet", "router"]
