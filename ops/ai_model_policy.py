"""Owner-side model metadata. Unknown prices/capabilities never authorize inference."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping

# Verified official standard-rate metadata, 22 October 2026. No model selection fallback.
OFFICIAL = {
    "gpt-6-luna": {"input_usd_per_million": 0.10, "output_usd_per_million": 0.50,
        "request_usd": 0, "context_window": 1050000, "max_output_tokens": 128000,
        "token_parameter": "max_completion_tokens", "reasoning_effort": "none",
        "structured_output": True, "vision": True,
        "source": "https://developers.openai.com/api/docs/models/gpt-6-luna"},
    "test-model": {"input_usd_per_million": 0.10, "output_usd_per_million": 0.50,
        "request_usd": 0, "context_window": 128000, "max_output_tokens": 16384,
        "token_parameter": "max_completion_tokens", "reasoning_effort": "none",
        "structured_output": True, "vision": False, "source": "offline-test-fixture"},
}


def _validate(value: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(value)
    for key in ("input_usd_per_million", "output_usd_per_million", "request_usd"):
        number = Decimal(str(data.get(key, 0)))
        if not number.is_finite() or not 0 <= number <= 1000:
            raise ValueError("Unbounded model price metadata")
        data[key] = float(number)
    for key in ("context_window", "max_output_tokens"):
        number = data.get(key)
        if isinstance(number, bool) or not isinstance(number, int) or not 128 <= number <= 10_000_000:
            raise ValueError("Invalid model token metadata")
    if data.get("token_parameter") not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError("Invalid compatible token parameter")
    if data.get("reasoning_effort") not in {None, "none"}:
        raise ValueError("Unbounded provider reasoning policy")
    if data.get("structured_output") not in {True, False} or data.get("vision") not in {True, False}:
        raise ValueError("Invalid model capability metadata")
    return data


def policy_for(profiles, profile_id: str, runtime=None):
    runtime = runtime or profiles.get_runtime(profile_id)
    # Test transports retain deterministic fixtures; live catalog never selects test-model.
    if runtime["model"] == "test-model" or (runtime["provider"] == "openai" and runtime["model"] in OFFICIAL):
        return dict(OFFICIAL[runtime["model"]])
    path = Path(profiles.state_dir) / "model-policies.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        row = data[profile_id]
        if (row["provider"], row["model"], row["endpoint_origin"]) != (runtime["provider"], runtime["model"], runtime["endpoint_origin"]):
            return None
        expiry = datetime.fromisoformat(row["valid_until"].replace("Z", "+00:00"))
        if expiry <= datetime.now(timezone.utc): return None
        return _validate(row["policy"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def register_reviewed_policy(profiles, profile_id: str, policy: Mapping[str, Any], *, valid_until: datetime):
    """Trusted owner setup only; never an SDK/model tool or secret-bearing response."""
    from ops.ai_fleet_settings import _atomic_write
    runtime = profiles.get_runtime(profile_id)
    clean = _validate(policy)
    now = datetime.now(timezone.utc)
    if valid_until.tzinfo is None or not now < valid_until <= now + timedelta(hours=24):
        raise ValueError("Model metadata must have an explicit bounded validity")
    path = Path(profiles.state_dir) / "model-policies.json"
    with profiles._lock:
        try: data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError: data = {}
        data[profile_id] = {"provider":runtime["provider"], "model":runtime["model"], "endpoint_origin":runtime["endpoint_origin"],
            "valid_until":valid_until.isoformat(), "policy":clean}
        _atomic_write(path, json.dumps(data, separators=(",", ":")).encode())
    return clean


async def refresh_openrouter_policy(profiles, profile_id: str, *, transport=None):
    """Owner-triggered public metadata check; does not send a key or perform inference."""
    import httpx
    runtime = profiles.get_runtime(profile_id)
    if runtime["provider"] != "openrouter" or runtime["endpoint_origin"] != "https://openrouter.ai/api/v1":
        raise ValueError("Only official OpenRouter catalogue discovery is automatic")
    from gitd.services.cloud_gateway import CloudGateway
    gateway = CloudGateway(profiles, transport=transport)
    gateway._check_dns(runtime["endpoint_origin"])
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, transport=transport) as client:
        async with client.stream("GET", "https://openrouter.ai/api/v1/models") as response:
            if response.status_code != 200: raise ValueError("Model catalogue unavailable")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 4 * 1024 * 1024: raise ValueError("Model catalogue exceeds bounds")
                body.extend(chunk)
    data = json.loads(body)
    rows = data.get("data", [])
    if not isinstance(rows, list) or len(rows) > 5000: raise ValueError("Invalid model catalogue")
    row = next((r for r in rows if isinstance(r, dict) and r.get("id") == runtime["model"]), None)
    if row is None: raise ValueError("Configured model is not listed")
    price = row.get("pricing", {})
    architecture = row.get("architecture", {})
    if "text" not in architecture.get("input_modalities", ["text"]) or "text" not in architecture.get("output_modalities", ["text"]):
        raise ValueError("Text Chat Completions capability is required")
    top = row.get("top_provider", {})
    supported = row.get("supported_parameters", [])
    policy = {"input_usd_per_million":float(Decimal(str(price["prompt"])) * 1000000),
        "output_usd_per_million":float(Decimal(str(price["completion"])) * 1000000),
        "request_usd":float(Decimal(str(price.get("request", "0")))),
        "context_window":int(row["context_length"]), "max_output_tokens":int(top.get("max_completion_tokens") or min(row["context_length"], 4096)),
        "token_parameter":"max_tokens", "reasoning_effort":"none" if row["id"] == "openai/gpt-6-luna" else None,
        "structured_output":"response_format" in supported, "vision":"image" in architecture.get("input_modalities", []),
        "source":"https://openrouter.ai/api/v1/models"}
    return register_reviewed_policy(profiles, profile_id, policy, valid_until=datetime.now(timezone.utc)+timedelta(hours=1))
