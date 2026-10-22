"""Secure server-side AI provider configuration.

The module deliberately keeps credentials out of the public representation.  The
state directory can be overridden with ``D3M0N_AI_SETTINGS_STATE_DIR`` for unit
tests; production defaults to the dashboard's owner-only state directory.
"""
from __future__ import annotations

import base64
import copy
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import threading
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from ops.platform_config import ensure_private_dir, ensure_private_file, state_dir as app_state_dir

STATE_DIR_ENV_NAMES = ("D3M0N_AI_SETTINGS_STATE_DIR", "AI_SETTINGS_STATE_DIR")
MAX_BODY_BYTES = 256 * 1024

DEFAULTS = {
    "context_length": 8192,
    "max_output_tokens": 1024,
    "max_turns": 12,
}

# These are compatibility defaults only.  Saved model lists are authoritative
# when present, and Ollama discovery may add installed models at request time.
_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "ollama": {"label": "Ollama (local)", "base_url": "http://localhost:11434", "default_model": "qwen3.5:4b", "models": ["qwen3.5:4b"], "requires_api_key": False, "notes": "Local Ollama server; model discovery is best effort."},
    "anthropic": {"label": "Claude API", "base_url": "https://api.anthropic.com", "default_model": "claude-sonnet-4-20250514", "models": ["claude-sonnet-4-20250514", "claude-opus-4-20250514"], "requires_api_key": True, "notes": "Uses the saved key, then ANTHROPIC_API_KEY."},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "default_model": "anthropic/claude-sonnet-4", "models": ["anthropic/claude-sonnet-4", "google/gemini-2.5-pro"], "requires_api_key": True, "notes": "Official OpenRouter endpoint only; model availability is account-dependent."},
    "claude-code": {"label": "Claude Code", "base_url": "", "default_model": "sonnet", "models": ["sonnet", "opus", "haiku"], "requires_api_key": False, "notes": "Requires the Claude Code CLI and its separate login/subscription."},
    "vllm": {"label": "vLLM (remote GPU)", "base_url": "http://127.0.0.1:8000/v1", "default_model": "unsloth/gemma-4-E4B-it", "models": ["unsloth/gemma-4-E2B-it", "unsloth/gemma-4-E2B-it-bnb-4bit", "unsloth/gemma-4-E4B-it", "unsloth/gemma-4-E4B-it-bnb-4bit"], "requires_api_key": False, "notes": "OpenAI-compatible local/private endpoint; no GPU setup is performed."},
    "on-device": {"label": "On-device (Gemma)", "base_url": "", "default_model": "gemma-3-1b-it", "models": ["gemma-3-1b-it", "gemma-2-2b-it", "gemma-4-e2b-q4km-gguf"], "requires_api_key": False, "notes": "Readiness depends on the Android on-device runtime."},
}

_ALLOWED = set(_PROVIDER_DEFAULTS)
_LOCAL_PROVIDERS = {"ollama", "vllm"}
_OFFICIAL_ORIGINS = {
    "anthropic": ("https", "api.anthropic.com", None),
    "openrouter": ("https", "openrouter.ai", "/api/v1"),
}
_LOCK = threading.RLock()


class SettingsError(RuntimeError):
    """Raised when encrypted settings cannot be safely read or validated."""


def state_dir() -> Path:
    override = next((os.environ.get(name, "").strip() for name in STATE_DIR_ENV_NAMES if os.environ.get(name, "").strip()), "")
    return Path(override).expanduser() if override else app_state_dir() / "ai-settings"


def key_path() -> Path:
    return state_dir() / "settings.key"


def settings_path() -> Path:
    return state_dir() / "settings.enc"


def _private_dir(path: Path) -> None:
    ensure_private_dir(path)


def _atomic_private_write(path: Path, data: bytes) -> None:
    _private_dir(path.parent)
    tmp = path.with_name(path.name + f".{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        ensure_private_file(tmp)
        os.replace(tmp, path)
        ensure_private_file(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _get_key(*, create: bool) -> bytes | None:
    path = key_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if not create:
            return None
        raw = Fernet.generate_key()
        _atomic_private_write(path, raw)
    except OSError as exc:
        raise SettingsError("AI settings storage unavailable") from exc
    if len(raw) != 44:
        raise SettingsError("AI settings storage unavailable")
    try:
        Fernet(raw)
    except Exception as exc:
        raise SettingsError("AI settings storage unavailable") from exc
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return raw


def _blank_settings() -> dict[str, Any]:
    try:
        from gitd.config import settings
        ollama_url = settings.ollama_base_url
        vllm_url = settings.vllm_base_url
    except Exception:
        ollama_url, vllm_url = "http://localhost:11434", "http://127.0.0.1:8000/v1"
    result = {"default_provider": "ollama", "default_model": "qwen3.5:4b", "defaults": dict(DEFAULTS), "providers": {}}
    for pid, info in _PROVIDER_DEFAULTS.items():
        item = copy.deepcopy(info)
        if pid == "ollama":
            item["base_url"] = ollama_url
        elif pid == "vllm":
            item["base_url"] = vllm_url
        result["providers"][pid] = item
    return result


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SettingsError("AI settings storage unavailable")
    result = _blank_settings()
    provider_values = raw.get("providers", {})
    if not isinstance(provider_values, dict):
        raise SettingsError("AI settings storage unavailable")
    for pid in _ALLOWED:
        saved = provider_values.get(pid, {})
        if not isinstance(saved, dict):
            raise SettingsError("AI settings storage unavailable")
        item = result["providers"][pid]
        for field in ("base_url", "default_model", "models", "api_key"):
            if field in saved:
                item[field] = copy.deepcopy(saved[field])
        if not isinstance(item.get("models"), list) or any(not isinstance(model, str) or not model.strip() for model in item["models"]):
            raise SettingsError("AI settings storage unavailable")
        item["models"] = list(dict.fromkeys(model.strip() for model in item["models"]))
        if "api_key" in item and not isinstance(item["api_key"], str):
            raise SettingsError("AI settings storage unavailable")
        if pid in _LOCAL_PROVIDERS or pid in _OFFICIAL_ORIGINS:
            validate_endpoint(pid, str(item.get("base_url", "")))
    provider = raw.get("default_provider", result["default_provider"])
    if provider not in _ALLOWED:
        raise SettingsError("AI settings storage unavailable")
    result["default_provider"] = provider
    result["default_model"] = str(raw.get("default_model", result["default_model"]))[:256]
    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        raise SettingsError("AI settings storage unavailable")
    for name, low, high in (("context_length", 1024, 131072), ("max_output_tokens", 64, 16384), ("max_turns", 1, 24)):
        value = defaults.get(name, DEFAULTS[name])
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise SettingsError("AI settings storage unavailable")
        result["defaults"][name] = value
    return result


def load_settings() -> dict[str, Any]:
    """Load a fresh decrypted snapshot; never cache credentials globally."""
    with _LOCK:
        path = settings_path()
        if not path.exists():
            if key_path().exists():
                raise SettingsError("AI settings storage unavailable")
            return _blank_settings()
        key = _get_key(create=False)
        if key is None:
            raise SettingsError("AI settings storage unavailable")
        try:
            plaintext = Fernet(key).decrypt(path.read_bytes(), ttl=None)
            raw = json.loads(plaintext.decode("utf-8"))
            return _normalise(raw)
        except (OSError, InvalidToken, ValueError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise SettingsError("AI settings storage unavailable") from exc


def save_settings(data: dict[str, Any]) -> None:
    normal = _normalise(data)
    with _LOCK:
        key = _get_key(create=True)
        assert key is not None
        payload = json.dumps(normal, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        _atomic_private_write(settings_path(), Fernet(key).encrypt(payload))


def validate_endpoint(provider: str, value: str) -> str:
    """Validate and return an endpoint without ever permitting credential URLs."""
    value = str(value or "").strip()
    if provider in {"claude-code", "on-device"} and not value:
        return ""
    if not value or len(value) > 512:
        raise ValueError("A valid endpoint is required")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid endpoint") from exc
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Endpoint must be http(s) without credentials, query, or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid endpoint")
    if provider in _LOCAL_PROVIDERS:
        host_lower = host.rstrip(".").lower()
        local_name = host_lower in {"localhost", "localhost.localdomain"}
        try:
            address = ipaddress.ip_address(host_lower)
            ranges = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]
            local_ip = address.is_loopback or any(address.version == n.version and address in n for n in ranges)
        except ValueError:
            local_ip = False
        # Literal private IPs avoid DNS rebinding; arbitrary .local names are not trusted.
        if not (local_name or local_ip):
            raise ValueError("Use localhost or a private IP address for local providers")
        if local_name:
            try:
                if any(not ipaddress.ip_address(row[4][0]).is_loopback for row in socket.getaddrinfo(host_lower, port or 80, type=socket.SOCK_STREAM)):
                    raise ValueError("Localhost must resolve to loopback")
            except OSError:
                raise ValueError("Localhost could not be resolved") from None
    elif provider in _OFFICIAL_ORIGINS:
        scheme, official_host, official_path = _OFFICIAL_ORIGINS[provider]
        if parsed.scheme != scheme or host.lower() != official_host or port not in (None, 443) or (official_path is not None and parsed.path.rstrip("/") != official_path):
            raise ValueError("Only the official provider endpoint is allowed")
        if official_path is None and parsed.path not in ("", "/"):
            raise ValueError("Only the official provider endpoint is allowed")
    return value.rstrip("/")


def _environment_key(provider: str) -> str:
    names = {"anthropic": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY", "vllm": "GITD_VLLM_API_KEY"}
    if provider not in names:
        return ""
    value = os.environ.get(names[provider], "")
    if value and value != "EMPTY":
        return value
    try:
        from gitd.config import settings
        attr = {"anthropic": "anthropic_api_key", "openrouter": "openrouter_api_key", "vllm": "vllm_api_key"}[provider]
        fallback = str(getattr(settings, attr, "") or "")
        return "" if fallback == "EMPTY" else fallback
    except Exception:
        return ""


def provider_runtime(provider: str) -> dict[str, Any]:
    data = load_settings()
    if provider not in _ALLOWED:
        raise ValueError("Unknown provider")
    item = copy.deepcopy(data["providers"][provider])
    item["api_key"] = item.get("api_key") or _environment_key(provider)
    return item


def default_provider() -> str:
    return load_settings()["default_provider"]


def _key_source(provider: str, item: dict[str, Any]) -> str:
    if item.get("api_key"):
        return "saved"
    if _environment_key(provider):
        return "environment"
    return "none" if item.get("requires_api_key") else "not-required"


def public_provider(provider: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    if provider not in _ALLOWED:
        raise ValueError("Unknown provider")
    snapshot = load_settings()
    item = copy.deepcopy(data or snapshot["providers"][provider])
    runtime = copy.deepcopy(item)
    runtime["api_key"] = item.get("api_key") or _environment_key(provider)
    return {
        "id": provider,
        "label": item["label"],
        "base_url": item.get("base_url", "") if provider not in {"claude-code", "on-device"} else "",
        "default_model": item.get("default_model", ""),
        "models": list(item.get("models", [])),
        "api_key_configured": bool(runtime.get("api_key")),
        "key_source": _key_source(provider, item),
        "requires_api_key": bool(item.get("requires_api_key")),
        "notes": item.get("notes", ""),
    }


def public_settings() -> dict[str, Any]:
    data = load_settings()
    return {"default_provider": data["default_provider"], "default_model": data["default_model"], "providers": [public_provider(pid, data["providers"][pid]) for pid in _PROVIDER_DEFAULTS], "defaults": dict(data["defaults"])}


def update_provider(provider: str, body: dict[str, Any]) -> dict[str, Any]:
    if provider not in _ALLOWED or not isinstance(body, dict):
        raise ValueError("Unknown provider or invalid body")
    data = load_settings()
    item = data["providers"][provider]
    unsupported = set(body) - {"base_url", "default_model", "models", "api_key", "clear_api_key"}
    if unsupported:
        raise ValueError("Unsupported settings field")
    if provider in {"claude-code", "on-device"} and any(name in body for name in ("base_url", "api_key", "clear_api_key")):
        raise ValueError("This provider does not support endpoint or API key settings")
    if "base_url" in body:
        item["base_url"] = validate_endpoint(provider, body["base_url"])
    if "default_model" in body:
        model = body["default_model"]
        if not isinstance(model, str) or not model.strip() or len(model.strip()) > 256:
            raise ValueError("Invalid default model")
        item["default_model"] = model.strip()
        if item["default_model"] not in item["models"]:
            item["models"].insert(0, item["default_model"])
    if "models" in body:
        models = body["models"]
        if not isinstance(models, list) or len(models) > 256 or any(not isinstance(m, str) or not m.strip() or len(m.strip()) > 256 for m in models):
            raise ValueError("Invalid model list")
        item["models"] = list(dict.fromkeys(m.strip() for m in models))
    if body.get("clear_api_key") is True:
        item.pop("api_key", None)
    elif "clear_api_key" in body and body["clear_api_key"] is not False:
        raise ValueError("clear_api_key must be boolean")
    if "api_key" in body and body.get("api_key") not in (None, ""):
        if not isinstance(body["api_key"], str) or len(body["api_key"]) > 4096:
            raise ValueError("Invalid API key")
        item["api_key"] = body["api_key"]
    save_settings(data)
    return public_provider(provider, data["providers"][provider])


def update_defaults(body: dict[str, Any]) -> None:
    if not isinstance(body, dict) or body.get("provider") not in _ALLOWED:
        raise ValueError("Invalid default provider")
    data = load_settings()
    provider = body["provider"]
    model = body.get("model")
    if not isinstance(model, str) or not model.strip() or len(model.strip()) > 256:
        raise ValueError("Invalid default model")
    data["default_provider"] = provider
    data["default_model"] = model.strip()
    for name, low, high in (("context_length", 1024, 131072), ("max_output_tokens", 64, 16384), ("max_turns", 1, 24)):
        if name in body:
            value = body[name]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"Invalid {name}")
            data["defaults"][name] = value
    save_settings(data)


def _safe_response_json(response: Any) -> dict[str, Any]:
    """Consume at most MAX_BODY_BYTES decompressed bytes, never response.content."""
    data = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=8192):
            if len(data) + len(chunk) > MAX_BODY_BYTES:
                raise ValueError("Provider metadata response exceeded the size limit")
            data.extend(chunk)
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Invalid provider metadata")
        return value
    finally:
        response.close()


class AISettingsBodyLimit:
    """Bound settings JSON before FastAPI parses it; no submitted secrets in errors."""
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/api/ai-settings") or scope.get("method") not in {"PUT", "POST"}:
            return await self.app(scope, receive, send)
        from starlette.responses import JSONResponse
        body = bytearray()
        headers = dict(scope.get("headers", []))
        try: length = int(headers.get(b"content-length", b"0"))
        except ValueError: length = MAX_BODY_BYTES + 1
        if length < 0 or length > MAX_BODY_BYTES:
            return await JSONResponse({"detail":"AI settings request too large"}, status_code=413, headers={"Cache-Control":"no-store"})(scope,receive,send)
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect": return
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY_BYTES:
                return await JSONResponse({"detail":"AI settings request too large"}, status_code=413, headers={"Cache-Control":"no-store"})(scope,receive,send)
            if not message.get("more_body", False): break
        sent = False
        async def replay():
            nonlocal sent
            if not sent:
                sent=True
                return {"type":"http.request", "body":bytes(body), "more_body":False}
            return await receive()
        return await self.app(scope,replay,send)


def install_ai_settings(app):
    load_settings()  # Validate the encrypted snapshot at process startup; no fallback on corruption.
    app.add_middleware(AISettingsBodyLimit)


def test_provider(provider: str, model: str = "") -> dict[str, Any]:
    import requests

    if provider not in _ALLOWED:
        raise ValueError("Unknown provider")
    item = provider_runtime(provider)
    if item.get("requires_api_key") and not item.get("api_key"):
        return {"ok": False, "status": "not_configured", "message": "API key is not configured"}
    if provider == "claude-code":
        executable = shutil.which("claude")
        if not executable:
            return {"ok": False, "status": "cli_missing", "message": "Claude Code CLI is not installed"}
        try:
            result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=3, check=False)
            if result.returncode == 0:
                return {"ok": True, "status": "cli_present", "message": "Claude Code CLI is installed; login/subscription is checked separately"}
            return {"ok": False, "status": "cli_error", "message": "Claude Code CLI is installed but did not report its version"}
        except Exception:
            return {"ok": False, "status": "cli_error", "message": "Claude Code CLI could not be inspected"}
    if provider == "on-device":
        try:
            from gitd.services.agent_chat_ondevice import _kotlin_llm
            ready = _kotlin_llm() is not None
        except Exception:
            ready = False
        return {"ok": ready, "status": "ready" if ready else "not_ready", "message": "On-device runtime is available" if ready else "On-device runtime is not available"}
    base = str(item.get("base_url", "")).rstrip("/")
    path = "/api/tags" if provider == "ollama" else "/auth/key" if provider == "openrouter" else "/models"
    if provider == "anthropic":
        path = "/v1/models"
    headers = {"Authorization": f"Bearer {item['api_key']}"} if item.get("api_key") else {}
    if provider == "anthropic":
        headers = {"x-api-key": item["api_key"], "anthropic-version": "2023-06-01"}
    try:
        response = requests.get(base + path, headers=headers, timeout=(2, 5), stream=True, allow_redirects=False)
        data = _safe_response_json(response)
        if response.status_code == 200:
            models = []
            raw = data.get("models") if provider == "ollama" else data.get("data")
            if isinstance(raw, list):
                models = [str(x.get("name") or x.get("id")) for x in raw if isinstance(x, dict) and (x.get("name") or x.get("id"))]
            if provider == "ollama" and (model or item.get("default_model")) not in models:
                return {"ok":False,"status":"model_missing","message":"Service is reachable, but the selected model is not installed","models":models}
            return {"ok": True, "status": "ready", "message": "Provider metadata endpoint responded; no inference was run", "models": models}
        if response.status_code in (401, 403):
            return {"ok": False, "status": "credentials_rejected", "message": "Provider rejected the configured credentials"}
        return {"ok": False, "status": "error", "message": "Provider metadata endpoint returned an error"}
    except requests.RequestException:
        return {"ok": False, "status": "unreachable", "message": "Provider metadata endpoint was not reachable"}
    except Exception:
        return {"ok": False, "status": "error", "message": "Provider metadata check failed"}
