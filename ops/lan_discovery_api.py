"""Additive phone discovery API.

The parent application should include ``router`` from this module after its
existing authenticated phone router.  This file intentionally does not edit or
register ``ops/server.py``.  Authentication, CSRF, and owner authorization stay
at the parent registration seam.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ops import lan_discovery

router = APIRouter(prefix="/api/phone/discovery", tags=["phone-discovery"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ScanBody(StrictBody):
    interface: str | None = Field(default=None, max_length=128)
    cidr: str | None = Field(default=None, max_length=43)
    methods: list[str] = Field(default_factory=lambda: ["mdns"], max_length=4)
    ports: list[int] = Field(default_factory=lambda: [5555], max_length=16)
    max_hosts: int = Field(default=lan_discovery.MAX_HOSTS, ge=1, le=lan_discovery.MAX_HOSTS)
    confirmed: bool = False


class EnrollBody(StrictBody):
    candidate_id: str = Field(min_length=8, max_length=128)
    action: str = Field(pattern="^(connect|pair)$")
    pairing_code: str | None = Field(default=None, min_length=1, max_length=64)


def _interfaces() -> list[dict[str, Any]]:
    return lan_discovery.list_ipv4_interfaces()


def _select_interfaces(rows: list[dict[str, Any]], name: str | None) -> list[dict[str, Any]]:
    if not name:
        return rows
    selected = [row for row in rows if row.get("name") == name]
    if not selected:
        raise HTTPException(status_code=400, detail="selected interface is not available")
    return selected


@router.get("/interfaces", summary="List local IPv4 discovery interfaces")
def discovery_interfaces() -> dict[str, Any]:
    return {"ok": True, "interfaces": _interfaces()}


@router.get("/status", summary="Read USB transport and passive mDNS status")
def discovery_status() -> dict[str, Any]:
    # This endpoint only runs adb devices/mdns metadata commands; it never
    # connects, pairs, changes tcpip mode, or touches a phone.
    return lan_discovery.discovery_status()


@router.post("/scan", summary="Passively discover mDNS services or probe one selected private LAN")
def discovery_scan(payload: ScanBody = Body(default_factory=ScanBody)) -> dict[str, Any]:
    methods = {method.lower().strip() for method in payload.methods}
    probe_requested = bool(methods & {"probe", "lan", "tcp"})
    if probe_requested and not payload.confirmed:
        raise HTTPException(status_code=400, detail="explicit confirmation is required for a private-LAN probe")
    if not methods or not methods <= {"mdns", "probe", "lan", "tcp"}:
        raise HTTPException(status_code=400, detail="methods must contain only mdns and probe")
    rows = _interfaces()
    selected = _select_interfaces(rows, payload.interface)
    if probe_requested and not payload.cidr:
        raise HTTPException(status_code=400, detail="select a private CIDR before probing")
    try:
        result = lan_discovery.scan_candidates(
            methods=methods,
            interfaces=selected,
            cidr=payload.cidr,
            ports=payload.ports,
            max_hosts=min(payload.max_hosts, lan_discovery.MAX_HOSTS),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.post("/enroll", summary="Explicitly connect or pair one issued discovery candidate")
def discovery_enroll(payload: EnrollBody = Body(...)) -> dict[str, Any]:
    try:
        result = lan_discovery.enroll_candidate(
            payload.candidate_id,
            action=payload.action,
            pairing_code=payload.pairing_code,
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # The service intentionally never returns or logs the submitted code.
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "enrollment failed"))
    return result


__all__ = ["EnrollBody", "ScanBody", "discovery_enroll", "discovery_interfaces", "discovery_scan", "discovery_status", "router"]
