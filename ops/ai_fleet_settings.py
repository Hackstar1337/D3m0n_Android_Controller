"""Encrypted, owner-controlled provider profiles for the AI fleet.

This namespace is intentionally separate from :mod:`ops.ai_settings`.  It never
reads environment credentials and never exposes a key in a public DTO.  The
store is suitable for local tests with a temporary directory; production callers
must supply the owner-only state directory used by the dashboard.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

try:
    from ai_contracts.models import ProfileRef, ProfileSnapshot, Role
except Exception:  # pragma: no cover - allows isolated module inspection
    ProfileRef = ProfileSnapshot = Role = None  # type: ignore[assignment]

from ops.platform_config import state_dir

DEFAULT_STATE_DIR = state_dir() / "ai-fleet" / "profiles"
STATE_DIR_ENV = "D3M0N_AI_FLEET_PROFILE_STATE_DIR"
MAX_PROFILES = 32
MAX_FREEZES = 64
MAX_MODEL_LENGTH = 200
MAX_ENDPOINT_LENGTH = 512
MAX_KEY_LENGTH = 8192
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_OFFICIAL = {
    "openai": ("api.openai.com", "/v1"),
    "openrouter": ("openrouter.ai", "/api/v1"),
}
_ROLES = ("planner", "executor", "verifier")
_COMPONENTS = {
    "manager",
    "executor",
    "fast_agent",
    "app_opener",
    "structured_output",
    "analysis_verifier",
    "sdk_agent",
}


class FleetProfileError(RuntimeError):
    """Base class for safe profile-store failures."""


class ProfileNotFoundError(FleetProfileError):
    pass


class ProfileExpiredError(FleetProfileError):
    pass


class ProfileRevokedError(FleetProfileError):
    pass


class EndpointPolicyError(FleetProfileError, ValueError):
    pass


class ProfileValidationError(FleetProfileError, ValueError):
    pass


@dataclass(frozen=True)
class ProfilePublic:
    profile_id: str
    provider: str
    model: str
    endpoint_origin: str
    roles: tuple[str, ...]
    capabilities: tuple[str, ...]
    configured: bool
    revoked: bool
    expires_at: str | None
    version: int
    max_tokens: int
    max_calls: int
    max_price_usd: str
    reserved_price_usd: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "provider": self.provider,
            "model": self.model,
            "endpoint_origin": self.endpoint_origin,
            "roles": list(self.roles),
            "capabilities": list(self.capabilities),
            "configured": self.configured,
            "revoked": self.revoked,
            "expires_at": self.expires_at,
            "version": self.version,
            "max_tokens": self.max_tokens,
            "max_calls": self.max_calls,
            "max_price_usd": self.max_price_usd,
            "reserved_price_usd": self.reserved_price_usd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ProfileFreeze:
    freeze_id: str
    version: int
    created_at: str
    role_bindings: dict[str, str]
    profiles: tuple[ProfilePublic, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "freeze_id": self.freeze_id,
            "version": self.version,
            "created_at": self.created_at,
            "role_bindings": dict(self.role_bindings),
            "profiles": [item.as_dict() for item in self.profiles],
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = _parse_time(value)
        return parsed.isoformat()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ProfileValidationError("Invalid profile expiry") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _private_dir(path: Path) -> None:
    from ops.platform_config import ensure_private_dir
    ensure_private_dir(path)


def _atomic_write(path: Path, payload: bytes) -> None:
    _private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        from ops.platform_config import ensure_private_file
        ensure_private_file(temporary)
        os.replace(temporary, path)
        ensure_private_file(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _default_resolver(host: str, port: int | None) -> list[str]:
    rows = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(row[4][0] for row in rows if row[4]))


def _public_dns(host: str, resolver: Callable[[str, int | None], Iterable[str]]) -> None:
    try:
        address = ipaddress.ip_address(host.rstrip("."))
        addresses = [str(address)]
    except ValueError:
        try:
            addresses = list(resolver(host, 443))
        except (OSError, socket.gaierror, ValueError) as exc:
            raise EndpointPolicyError("Endpoint DNS resolution failed") from exc
    if not addresses:
        raise EndpointPolicyError("Endpoint DNS resolution failed")
    for raw in addresses:
        try:
            address = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError as exc:
            raise EndpointPolicyError("Endpoint DNS resolution failed") from exc
        if not address.is_global:
            raise EndpointPolicyError("Endpoint must resolve only to public addresses")


def normalize_endpoint(
    endpoint: str,
    provider: str,
    *,
    approved_generic_origins: Iterable[str] = (),
    resolver: Callable[[str, int | None], Iterable[str]] = _default_resolver,
) -> str:
    """Validate an HTTPS endpoint against official or owner-approved origins.

    DNS is checked at profile creation and again by the gateway before a request;
    callers can inject a fake resolver for deterministic offline tests.
    """
    if not isinstance(endpoint, str) or len(endpoint.strip()) > MAX_ENDPOINT_LENGTH:
        raise EndpointPolicyError("Invalid provider endpoint")
    value = endpoint.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise EndpointPolicyError("Invalid provider endpoint") from exc
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise EndpointPolicyError("Provider endpoint must be HTTPS without credentials, query, or fragment")
    if port not in (None, 443):
        raise EndpointPolicyError("Provider endpoint must use HTTPS port 443")
    host = host.rstrip(".").lower()
    path = parsed.path.rstrip("/") or ""
    def canonical_origin(raw: str) -> str:
        try:
            candidate = urlsplit(str(raw).strip().rstrip("/"))
            candidate_host = candidate.hostname
            candidate_port = candidate.port
        except ValueError as exc:
            raise EndpointPolicyError("Invalid approved endpoint") from exc
        if candidate.scheme != "https" or not candidate_host or candidate.username or candidate.password or candidate.query or candidate.fragment or candidate_port not in (None, 443):
            raise EndpointPolicyError("Approved endpoint must be HTTPS without credentials, query, or fragment")
        return urlunsplit(("https", candidate_host.rstrip(".").lower(), candidate.path.rstrip("/") or "", "", ""))

    if provider in _OFFICIAL:
        official_host, official_path = _OFFICIAL[provider]
        if host != official_host or path != official_path:
            raise EndpointPolicyError("Only the official provider endpoint is allowed")
    elif provider == "generic":
        canonical = urlunsplit(("https", host, path, "", ""))
        allowed = {canonical_origin(item) for item in approved_generic_origins}
        if canonical not in allowed:
            raise EndpointPolicyError("Generic endpoint is not owner-approved")
    else:
        raise ProfileValidationError("Unsupported provider")
    _public_dns(host, resolver)
    return urlunsplit(("https", host, path, "", ""))


def _safe_profile_id(profile_id: str) -> str:
    if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
        raise ProfileValidationError("Invalid profile name")
    return profile_id


def _safe_expiry(value: datetime | str | None) -> str | None:
    result = _iso(value)
    if result is not None and _parse_time(result) <= _now():
        raise ProfileValidationError("Profile expiry must be in the future")
    return result


class ProfileStore:
    """Encrypted named profile catalog with no environment fallback."""

    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        approved_generic_origins: Iterable[str] = (),
        resolver: Callable[[str, int | None], Iterable[str]] = _default_resolver,
        max_profiles: int = MAX_PROFILES,
    ) -> None:
        configured = state_dir or os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR
        self.state_dir = Path(configured).expanduser()
        self.key_file = self.state_dir / "profiles.key"
        self.data_file = self.state_dir / "profiles.enc"
        self.approved_generic_origins = tuple(approved_generic_origins)
        self.resolver = resolver
        self.max_profiles = max(1, min(int(max_profiles), MAX_PROFILES))
        self._lock = threading.RLock()

    def _key(self, create: bool) -> bytes | None:
        try:
            raw = self.key_file.read_bytes()
        except FileNotFoundError:
            if not create:
                return None
            raw = Fernet.generate_key()
            _atomic_write(self.key_file, raw)
        except OSError as exc:
            raise FleetProfileError("Fleet profile storage unavailable") from exc
        try:
            Fernet(raw)
        except Exception as exc:
            raise FleetProfileError("Fleet profile storage unavailable") from exc
        try:
            from ops.platform_config import ensure_private_file
            ensure_private_file(self.key_file)
        except OSError:
            raise FleetProfileError("Fleet profile storage unavailable")
        return raw

    def _blank(self) -> dict[str, Any]:
        return {"schema": 1, "revision": 0, "profiles": {}, "freezes": []}

    def _load(self) -> dict[str, Any]:
        if not self.data_file.exists():
            if self.key_file.exists():
                raise FleetProfileError("Fleet profile storage unavailable")
            return self._blank()
        key = self._key(False)
        if key is None:
            raise FleetProfileError("Fleet profile storage unavailable")
        try:
            raw = json.loads(Fernet(key).decrypt(self.data_file.read_bytes()).decode("utf-8"))
        except (OSError, InvalidToken, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise FleetProfileError("Fleet profile storage unavailable") from exc
        if not isinstance(raw, dict) or raw.get("schema") != 1 or not isinstance(raw.get("profiles"), dict):
            raise FleetProfileError("Fleet profile storage unavailable")
        raw.setdefault("revision", 0)
        raw.setdefault("freezes", [])
        return raw

    def _save(self, data: dict[str, Any]) -> None:
        key = self._key(True)
        assert key is not None
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        _atomic_write(self.data_file, Fernet(key).encrypt(payload))

    def _public(self, item: Mapping[str, Any], *, now: datetime | None = None) -> ProfilePublic:
        now = now or _now()
        expiry = item.get("expires_at")
        expired = bool(expiry and _parse_time(expiry) <= now)
        return ProfilePublic(
            profile_id=item["profile_id"], provider=item["provider"], model=item["model"],
            endpoint_origin=item["endpoint_origin"], roles=tuple(item.get("roles", ())),
            capabilities=tuple(item.get("capabilities", ())),
            configured=bool(item.get("secret")) and not bool(item.get("revoked")) and not expired,
            revoked=bool(item.get("revoked")), expires_at=expiry, version=int(item["version"]),
            max_tokens=int(item["max_tokens"]), max_calls=int(item["max_calls"]),
            max_price_usd=item["max_price_usd"], reserved_price_usd=item.get("reserved_price_usd", "0"),
            created_at=item["created_at"], updated_at=item["updated_at"],
        )

    def list_profiles(self) -> tuple[ProfilePublic, ...]:
        with self._lock:
            data = self._load()
            return tuple(self._public(data["profiles"][pid]) for pid in sorted(data["profiles"]))

    def get_profile(self, profile_id: str) -> ProfilePublic:
        profile_id = _safe_profile_id(profile_id)
        with self._lock:
            item = self._load()["profiles"].get(profile_id)
            if item is None:
                raise ProfileNotFoundError("Fleet profile not found")
            return self._public(item)

    def get_runtime(self, profile_id: str) -> dict[str, Any]:
        """Return secret-bearing data only to trusted server-side gateway code."""
        profile_id = _safe_profile_id(profile_id)
        with self._lock:
            item = self._load()["profiles"].get(profile_id)
            if item is None:
                raise ProfileNotFoundError("Fleet profile not found")
            if item.get("revoked"):
                raise ProfileRevokedError("Fleet profile is revoked")
            if item.get("expires_at") and _parse_time(item["expires_at"]) <= _now():
                raise ProfileExpiredError("Fleet profile has expired")
            if not item.get("secret"):
                raise ProfileValidationError("Fleet profile is not configured")
            return copy.deepcopy(item)

    def create_profile(
        self,
        profile_id: str,
        provider: str,
        model: str,
        endpoint: str,
        secret: str,
        *,
        roles: Iterable[str] = (),
        capabilities: Iterable[str] = (),
        expires_at: datetime | str | None = None,
        max_tokens: int = 4096,
        max_calls: int = 1000,
        max_price_usd: str = "20",
    ) -> ProfilePublic:
        profile_id = _safe_profile_id(profile_id)
        provider = str(provider).strip().lower()
        if provider not in {*_OFFICIAL, "generic"}:
            raise ProfileValidationError("Unsupported provider")
        if not isinstance(model, str) or not model.strip() or len(model.strip()) > MAX_MODEL_LENGTH:
            raise ProfileValidationError("Invalid model")
        if not isinstance(secret, str) or not secret or len(secret) > MAX_KEY_LENGTH:
            raise ProfileValidationError("Invalid provider secret")
        normalized = normalize_endpoint(endpoint, provider, approved_generic_origins=self.approved_generic_origins, resolver=self.resolver)
        role_values = tuple(sorted(set(str(value).strip().lower() for value in roles)))
        if any(role not in _ROLES for role in role_values):
            raise ProfileValidationError("Invalid profile role")
        capability_values = tuple(sorted(set(str(value).strip()[:80] for value in capabilities if str(value).strip())))
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 16384:
            raise ProfileValidationError("Invalid token limit")
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or not 1 <= max_calls <= 1000:
            raise ProfileValidationError("Invalid call limit")
        try:
            from decimal import Decimal
            price = Decimal(str(max_price_usd))
        except Exception as exc:
            raise ProfileValidationError("Invalid spend limit") from exc
        if price < 0 or price > 20:
            raise ProfileValidationError("Spend limit exceeds USD 20 ceiling")
        expiry = _safe_expiry(expires_at)
        with self._lock:
            data = self._load()
            existing = data["profiles"].get(profile_id)
            if existing is None and len(data["profiles"]) >= self.max_profiles:
                raise ProfileValidationError("Profile catalog is full")
            now = _now().isoformat()
            version = int(existing["version"]) + 1 if existing else 1
            item = {
                "profile_id": profile_id, "provider": provider, "model": model.strip(),
                "endpoint_origin": normalized, "secret": secret, "roles": list(role_values),
                "capabilities": list(capability_values), "revoked": False, "expires_at": expiry,
                "version": version, "max_tokens": max_tokens, "max_calls": max_calls,
                "max_price_usd": format(price, "f"), "reserved_price_usd": "0",
                "created_at": existing.get("created_at", now) if existing else now, "updated_at": now,
                "calls_reserved": 0,
            }
            data["profiles"][profile_id] = item
            data["revision"] = int(data.get("revision", 0)) + 1
            self._save(data)
            return self._public(item)

    save_profile = create_profile
    public_profile = get_profile

    def revoke_profile(self, profile_id: str) -> ProfilePublic:
        return self._set_lifecycle(profile_id, revoked=True)

    revoke = revoke_profile

    def clear_secret(self, profile_id: str) -> ProfilePublic:
        profile_id = _safe_profile_id(profile_id)
        with self._lock:
            data = self._load()
            item = data["profiles"].get(profile_id)
            if item is None:
                raise ProfileNotFoundError("Fleet profile not found")
            item["secret"] = ""
            item["updated_at"] = _now().isoformat()
            item["version"] = int(item["version"]) + 1
            data["revision"] = int(data.get("revision", 0)) + 1
            self._save(data)
            return self._public(item)

    clear_profile_secret = clear_secret

    def _set_lifecycle(self, profile_id: str, *, revoked: bool) -> ProfilePublic:
        profile_id = _safe_profile_id(profile_id)
        with self._lock:
            data = self._load()
            item = data["profiles"].get(profile_id)
            if item is None:
                raise ProfileNotFoundError("Fleet profile not found")
            item["revoked"] = revoked
            item["updated_at"] = _now().isoformat()
            item["version"] = int(item["version"]) + 1
            data["revision"] = int(data.get("revision", 0)) + 1
            self._save(data)
            return self._public(item)

    def purge_expired(self, *, now: datetime | None = None, retain_freezes: int = 8) -> int:
        """Remove expired profiles and old frozen metadata; never touches active profiles."""
        now = now or _now()
        with self._lock:
            data = self._load()
            before = len(data["profiles"])
            data["profiles"] = {
                pid: item for pid, item in data["profiles"].items()
                if not item.get("expires_at") or _parse_time(item["expires_at"]) > now
            }
            freezes = list(data.get("freezes", []))
            keep = max(0, min(retain_freezes, MAX_FREEZES))
            data["freezes"] = freezes[-keep:] if keep else []
            removed = before - len(data["profiles"])
            if removed or len(data["freezes"]) != len(freezes):
                data["revision"] = int(data.get("revision", 0)) + 1
                self._save(data)
            return removed

    def freeze(self, role_bindings: Mapping[str, str], *, profile_ids: Iterable[str] | None = None) -> ProfileFreeze:
        bindings = {str(key).lower(): _safe_profile_id(str(value)) for key, value in role_bindings.items()}
        if set(bindings) != set(_ROLES):
            raise ProfileValidationError("planner, executor, and verifier role bindings are required")
        with self._lock:
            data = self._load()
            selected = set(profile_ids or bindings.values())
            if not selected <= set(data["profiles"]):
                raise ProfileNotFoundError("Fleet profile not found")
            items = [data["profiles"][pid] for pid in sorted(selected)]
            for item in items:
                if self._public(item).configured is False:
                    raise ProfileValidationError("All frozen profiles must be configured and active")
            for role, profile_id in bindings.items():
                if role not in data["profiles"][profile_id].get("roles", []):
                    raise ProfileValidationError("Frozen role binding is not declared by the profile")
            version = int(data.get("revision", 0)) + 1
            now = _now().isoformat()
            freeze = {"freeze_id": secrets.token_urlsafe(12), "version": version, "created_at": now,
                      "role_bindings": bindings, "profile_ids": sorted(selected),
                      "profile_versions": {item["profile_id"]: item["version"] for item in items},
                      "public_profiles": [self._public(item).as_dict() for item in items]}
            data.setdefault("freezes", []).append(freeze)
            data["freezes"] = data["freezes"][-MAX_FREEZES:]
            data["revision"] = version
            self._save(data)
            return ProfileFreeze(freeze["freeze_id"], version, now, dict(bindings), tuple(self._public(item) for item in items))

    freeze_profiles = freeze

    def latest_freeze(self) -> ProfileFreeze | None:
        with self._lock:
            data = self._load()
            rows = data.get("freezes", [])
            if not rows:
                return None
            row = rows[-1]
            if row.get("public_profiles"):
                profiles = tuple(ProfilePublic(**item) for item in row["public_profiles"])
                return ProfileFreeze(row["freeze_id"], int(row["version"]), row["created_at"], dict(row["role_bindings"]), profiles)
            items = [data["profiles"].get(pid) for pid in row["profile_ids"]]
            items = [item for item in items if item is not None]
            return ProfileFreeze(row["freeze_id"], int(row["version"]), row["created_at"], dict(row["role_bindings"]), tuple(self._public(item) for item in items))

    def reserve(self, profile_id: str, *, calls: int = 1, amount_usd: str = "0") -> None:
        """Reserve bounded calls/spend before a network operation; idempotency is gateway-owned."""
        if calls < 1 or calls > 100:
            raise ProfileValidationError("Invalid call reservation")
        from decimal import Decimal
        try:
            amount = Decimal(str(amount_usd))
        except Exception as exc:
            raise ProfileValidationError("Invalid spend reservation") from exc
        if amount < 0:
            raise ProfileValidationError("Invalid spend reservation")
        with self._lock:
            data = self._load()
            item = data["profiles"].get(_safe_profile_id(profile_id))
            if item is None:
                raise ProfileNotFoundError("Fleet profile not found")
            self.get_runtime(profile_id)
            if int(item.get("calls_reserved", 0)) + calls > int(item["max_calls"]):
                raise ProfileValidationError("Profile call budget exhausted")
            siblings = [row for row in data["profiles"].values() if row.get("secret") == item.get("secret") and row.get("provider") == item.get("provider") and row.get("endpoint_origin") == item.get("endpoint_origin")]
            account_limit = min(Decimal(row["max_price_usd"]) for row in siblings)
            account_reserved = sum((Decimal(row.get("reserved_price_usd", "0")) for row in siblings), Decimal(0))
            if account_reserved + amount > account_limit:
                raise ProfileValidationError("Shared-key spend budget exhausted")
            if Decimal(item["reserved_price_usd"]) + amount > Decimal(item["max_price_usd"]):
                raise ProfileValidationError("Profile spend budget exhausted")
            item["calls_reserved"] = int(item.get("calls_reserved", 0)) + calls
            item["reserved_price_usd"] = format(Decimal(item["reserved_price_usd"]) + amount, "f")
            data["revision"] = int(data.get("revision", 0)) + 1
            self._save(data)

    def public_catalog(self) -> tuple[dict[str, Any], ...]:
        return tuple(item.as_dict() for item in self.list_profiles())


def profile_snapshot(store: ProfileStore, freeze: ProfileFreeze | None = None) -> Any:
    """Convert a public freeze to the foundation's immutable ``ProfileSnapshot``."""
    if freeze is None:
        freeze = store.latest_freeze()
    if freeze is None or ProfileSnapshot is None:
        raise ProfileValidationError("No profile freeze is available")
    refs = tuple(ProfileRef(profile_id=item.profile_id, provider=item.provider, model=item.model,
                            endpoint_origin=item.endpoint_origin, configured=item.configured,
                            expires_at=_parse_time(item.expires_at) if item.expires_at else None,
                            capabilities=frozenset(item.capabilities)) for item in freeze.profiles)
    return ProfileSnapshot(profiles=refs, role_bindings=freeze.role_bindings, version=str(freeze.version))


__all__ = [
    "EndpointPolicyError", "FleetProfileError", "ProfileExpiredError", "ProfileFreeze",
    "ProfileNotFoundError", "ProfilePublic", "ProfileRevokedError", "ProfileStore",
    "ProfileValidationError", "normalize_endpoint", "profile_snapshot",
]
