"""Persistent, human-friendly phone numbers (1, 2, 3, ...) for the fleet.

Numbers are stored in the existing fleet SQLite database so every browser sees
the same "phone 7".  A number is assigned once and kept when a phone goes
offline, so it comes back with the same number.  New phones receive the
smallest free number; a first batch is numbered in natural IP order so
the first reachable endpoint is numbered 1, then 2, and so on.

Mutating routes reuse the fleet same-origin guard; the dashboard password gate
in ``ops.dashboard_auth`` protects every ``/api`` route as a whole.
"""
from __future__ import annotations

import ipaddress
import re
import sqlite3
from contextlib import closing
from typing import Any, Iterable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from ops import fleet_control

router = APIRouter(prefix="/api/fleet/numbers", tags=["fleet"])

_SERIAL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,150}$")
_MAX_SERIALS = 500
_MAX_NUMBER = 9999


class DevicesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    devices: list[str]


class NumberBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: int


def _error(message: str, status: int = 422) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def _validate_serial(serial: Any) -> str:
    if not isinstance(serial, str) or not _SERIAL_RE.fullmatch(serial):
        raise _error("Device serial must match [A-Za-z0-9_.:-] and be 1 to 150 characters")
    return serial


def _validate_serials(devices: Any) -> list[str]:
    if not isinstance(devices, list):
        raise _error("devices must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for serial in devices:
        serial = _validate_serial(serial)
        if serial not in seen:
            seen.add(serial)
            result.append(serial)
    if len(result) > _MAX_SERIALS:
        raise _error(f"At most {_MAX_SERIALS} devices can be numbered per request")
    return result


def natural_key(serial: str) -> tuple:
    """Order wireless serials by IP then port; everything else after, by text."""
    host, sep, port = serial.rpartition(":")
    if not sep:
        host, port = serial, ""
    try:
        ip = ipaddress.ip_address(host)
        return (0, ip.version, int(ip), int(port) if port.isdigit() else 0, serial)
    except ValueError:
        return (1, 0, 0, 0, serial.casefold())


def _open() -> sqlite3.Connection:
    connection = fleet_control._open_db()
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS device_numbers (
            serial TEXT PRIMARY KEY,
            number INTEGER NOT NULL UNIQUE,
            assigned_at TEXT NOT NULL
        )
        """
    )
    return connection


def _read_all(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute("SELECT serial, number FROM device_numbers ORDER BY number").fetchall()
    return {row["serial"]: int(row["number"]) for row in rows}


def _assign_missing(connection: sqlite3.Connection, serials: Iterable[str]) -> list[str]:
    current = _read_all(connection)
    used = set(current.values())
    missing = sorted((s for s in serials if s not in current), key=natural_key)
    now = fleet_control._utc_now()
    candidate = 1
    for serial in missing:
        while candidate in used:
            candidate += 1
        if candidate > _MAX_NUMBER:
            raise _error("No free phone numbers are left", 409)
        connection.execute(
            "INSERT INTO device_numbers (serial, number, assigned_at) VALUES (?, ?, ?)",
            (serial, candidate, now),
        )
        used.add(candidate)
    return missing


def _payload(connection: sqlite3.Connection, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "numbers": _read_all(connection), **extra}


@router.get("")
async def list_numbers() -> dict[str, Any]:
    with closing(_open()) as connection:
        return _payload(connection)


@router.post("/assign", dependencies=[Depends(fleet_control._same_origin)])
async def assign_numbers(body: DevicesBody) -> dict[str, Any]:
    serials = _validate_serials(body.devices)
    with closing(_open()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            assigned = _assign_missing(connection, serials)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return _payload(connection, assigned=assigned)


@router.put("/{serial}", dependencies=[Depends(fleet_control._same_origin)])
async def set_number(serial: str, body: NumberBody) -> dict[str, Any]:
    """Give one phone a specific number; a phone already holding it swaps."""
    serial = _validate_serial(serial)
    number = body.number
    if not isinstance(number, int) or not 1 <= number <= _MAX_NUMBER:
        raise _error(f"Number must be between 1 and {_MAX_NUMBER}")
    with closing(_open()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = _read_all(connection)
            previous = current.get(serial)
            holder = next((s for s, n in current.items() if n == number and s != serial), None)
            now = fleet_control._utc_now()
            swapped = None
            if previous != number:
                # Park the holder on a temporary negative number to satisfy UNIQUE.
                if holder is not None:
                    connection.execute("UPDATE device_numbers SET number = -1 WHERE serial = ?", (holder,))
                connection.execute(
                    "INSERT INTO device_numbers (serial, number, assigned_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(serial) DO UPDATE SET number = excluded.number, assigned_at = excluded.assigned_at",
                    (serial, number, now),
                )
                if holder is not None:
                    if previous is None:
                        used = set(_read_all(connection).values())
                        previous = next(n for n in range(1, _MAX_NUMBER + 2) if n not in used)
                    connection.execute(
                        "UPDATE device_numbers SET number = ?, assigned_at = ? WHERE serial = ?",
                        (previous, now, holder),
                    )
                    swapped = {"device": holder, "number": previous}
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return _payload(connection, swapped=swapped)


@router.post("/reset", dependencies=[Depends(fleet_control._same_origin)])
async def reset_numbers(body: DevicesBody) -> dict[str, Any]:
    """Renumber the given phones 1..N in IP order and forget all other numbers."""
    serials = _validate_serials(body.devices)
    if not serials:
        raise _error("Provide the phones to renumber")
    with closing(_open()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DELETE FROM device_numbers")
            _assign_missing(connection, serials)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return _payload(connection)
