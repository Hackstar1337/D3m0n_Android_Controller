"""Agent Chat routes — interactive natural language phone control."""

import json
import logging
import threading
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse, StreamingResponse
from ops.platform_config import get_allowed_origins

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent-chat", tags=["agent-chat"])
ai_settings_router = APIRouter(prefix="/api/ai-settings", tags=["ai-settings"])

_AI_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_AI_SAME_ORIGINS = get_allowed_origins()


def _ai_access(request: Request, *, write: bool = False) -> None:
    """Require the dashboard middleware's local/session decision.

    The fallback direct check keeps unit-created FastAPI apps safe too; it does
    not trust arbitrary callers merely because they are on a private network.
    """
    from ops.dashboard_auth import is_trusted_local, session_cookie, state

    local = is_trusted_local(request.scope)
    authenticated = request.scope.get("d3m0n_auth") in {"local", "session"}
    if not authenticated and not local:
        config = state.config()
        token = session_cookie(request.scope)
        authenticated = bool(config and token and config.token_valid(token))
    if not authenticated:
        raise HTTPException(status_code=401, detail="Dashboard session required", headers=_AI_NO_STORE)
    if write and not local:
        origin = request.headers.get("origin", "").strip()
        fetch_site = request.headers.get("sec-fetch-site", "").lower().strip()
        if origin and origin not in get_allowed_origins():
            raise HTTPException(status_code=403, detail="Same-origin request required", headers=_AI_NO_STORE)
        if fetch_site == "cross-site":
            raise HTTPException(status_code=403, detail="Same-origin request required", headers=_AI_NO_STORE)


def _ai_json(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=_AI_NO_STORE)


@ai_settings_router.get("")
def ai_settings_get(request: Request):
    _ai_access(request)
    from ops.ai_settings import SettingsError, public_settings
    try:
        return _ai_json(public_settings())
    except SettingsError:
        raise HTTPException(status_code=503, detail="AI settings storage unavailable", headers=_AI_NO_STORE)


@ai_settings_router.put("/default")
def ai_settings_default(request: Request, body: dict = Body(...)):
    _ai_access(request, write=True)
    from ops.ai_settings import SettingsError, update_defaults
    try:
        update_defaults(body)
        return _ai_json({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc), headers=_AI_NO_STORE) from None
    except SettingsError:
        raise HTTPException(status_code=503, detail="AI settings storage unavailable", headers=_AI_NO_STORE)


@ai_settings_router.put("/{provider}")
def ai_settings_update(provider: str, request: Request, body: dict = Body(...)):
    _ai_access(request, write=True)
    from ops.ai_settings import SettingsError, update_provider
    try:
        return _ai_json(update_provider(provider, body))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc), headers=_AI_NO_STORE) from None
    except SettingsError:
        raise HTTPException(status_code=503, detail="AI settings storage unavailable", headers=_AI_NO_STORE)


@ai_settings_router.post("/{provider}/test")
def ai_settings_test(provider: str, request: Request, body: dict = Body({})):
    _ai_access(request, write=True)
    from ops.ai_settings import SettingsError, test_provider
    model = body.get("model", "") if isinstance(body, dict) else ""
    try:
        return _ai_json(test_provider(provider, model=model))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc), headers=_AI_NO_STORE) from None
    except SettingsError:
        raise HTTPException(status_code=503, detail="AI settings storage unavailable", headers=_AI_NO_STORE)

# ── Request models ──────────────────────────────────────────────────────────


class OllamaModelRequest(BaseModel):
    model: str


@router.post("/session", summary="Create Agent Chat Session")
def create_session(data: dict = Body({})):
    """Create a new agent chat session for a device."""
    from gitd.services.agent_chat import create_session

    device = data.get("device", "")
    if not device:
        raise HTTPException(status_code=400, detail="device required")
    session = create_session(
        device=device,
        provider=data.get("provider", ""),
        model=data.get("model", ""),
        system_prompt=data.get("system_prompt", ""),
    )
    return {"ok": True, "session_id": session.id, "device": session.device, "model": session.model}


@router.get("/sessions", summary="List Active Sessions")
def list_sessions(request: Request):
    _ai_access(request)
    from gitd.services.agent_chat import list_sessions

    return _ai_json(list_sessions())


@router.get("/session/{sid}", summary="Get Session History")
def get_session(sid: str):
    from gitd.services.agent_chat import get_session

    session = get_session(sid)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "id": session.id,
        "device": session.device,
        "model": session.model,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "tool_name": m.tool_name,
                "tool_args": m.tool_args,
                "image_b64": m.image_b64[:100] if m.image_b64 else "",
            }
            for m in session.messages
        ],
    }


@router.delete("/session/{sid}", summary="Delete Session")
def delete_session(sid: str):
    from gitd.services.agent_chat import delete_session

    delete_session(sid)
    return {"ok": True}


@router.post("/stop/{sid}", summary="Stop Running Agent")
def stop_agent(sid: str, data: dict = Body({})):
    """Cancel this session run; an optional run_id protects against stale Stop requests."""
    from gitd.services.agent_chat import stop_agent

    run_id = data.get("run_id", "") if isinstance(data, dict) else ""
    stop_agent(sid, run_id=str(run_id or ""))
    return {"ok": True}


@router.post("/message", summary="Send Message (SSE Stream)")
def send_message(data: dict = Body({})):
    """Send a message to the agent. Returns SSE stream of events."""
    from gitd.services.agent_chat import (
        chat_turn,
        create_session,
        get_session,
        save_session_to_db,
        begin_agent_run,
        end_agent_run,
        stop_agent as cancel_agent_run,
        _run_is_current,
    )

    sid = data.get("session_id", "")
    message = data.get("content", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="content required")

    session = get_session(sid) if sid else None
    # Session not in memory — try to reload from DB (happens after backend restart)
    if not session and sid:
        from gitd.services.agent_chat import load_conversation

        session = load_conversation(sid)
    # Auto-create session if device provided but no session
    if not session:
        device = data.get("device", "")
        if not device:
            raise HTTPException(status_code=400, detail="session_id or device required")
        session = create_session(
            device=device,
            provider=data.get("provider", ""),
            model=data.get("model", ""),
        )

    if data.get("device") and data["device"] != session.device:
        raise HTTPException(status_code=409, detail="Session belongs to a different phone; start a new conversation")
    if "auto_screenshot" in data and not isinstance(data["auto_screenshot"], bool):
        raise HTTPException(status_code=400, detail="auto_screenshot must be boolean")
    run_id = data.get("run_id", "")
    if not isinstance(run_id, str) or len(run_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid run_id")
    try:
        begin_agent_run(session, run_id=run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    run_id = session.current_run_id
    if "auto_screenshot" in data:
        session.auto_screenshot = data["auto_screenshot"]

    def generate():
        try:
            # The run is reserved BEFORE exposing its session id, closing the Stop race.
            yield f"data: {json.dumps({'type': 'session', 'session_id': session.id})}\n\n"
            for event in chat_turn(
                session,
                message,
                context_length=data.get("context_length", 8192),
                max_output_tokens=data.get("max_output_tokens", 1024),
                max_turns=data.get("max_turns", 12),
                _reserved=True,
                _run_id=run_id,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except (GeneratorExit, Exception) as e:
            if not isinstance(e, GeneratorExit):
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
        finally:
            # ALWAYS clean up — whether stream completed normally or was aborted
            try:
                cancel_agent_run(session.id, run_id=run_id)
            except Exception:
                pass
            try:
                if _run_is_current(session, run_id):
                    save_session_to_db(session)
            except Exception:
                pass
            finally:
                end_agent_run(session, run_id=run_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Conversation persistence endpoints ─────────────────────────────────────


@router.get("/conversations", summary="List Saved Conversations")
def list_conversations_endpoint(device: Optional[str] = Query(None)):
    """List saved conversations, optionally filtered by device."""
    from gitd.services.agent_chat import list_conversations

    return list_conversations(device=device)


@router.post("/conversation/{cid}/resume", summary="Resume Conversation")
def resume_conversation(cid: str):
    """Load a saved conversation into an active session so it can be continued."""
    from gitd.services.agent_chat import get_session, load_conversation

    # If already loaded in memory, just return it
    existing = get_session(cid)
    if existing:
        return {
            "ok": True,
            "session_id": existing.id,
            "device": existing.device,
            "provider": existing.provider,
            "model": existing.model,
            "message_count": len(existing.messages),
            "messages": [
                {"role": m.role, "content": m.content, "tool_name": m.tool_name, "tool_args": m.tool_args}
                for m in existing.messages
            ],
        }

    session = load_conversation(cid)
    if not session:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {
        "ok": True,
        "session_id": session.id,
        "device": session.device,
        "provider": session.provider,
        "model": session.model,
        "message_count": len(session.messages),
        "messages": [
            {"role": m.role, "content": m.content, "tool_name": m.tool_name, "tool_args": m.tool_args}
            for m in session.messages
        ],
    }


@router.delete("/conversation/{cid}", summary="Delete Conversation")
def delete_conversation_endpoint(cid: str):
    """Delete a conversation and its messages from the database."""
    from gitd.services.agent_chat import delete_conversation

    delete_conversation(cid)
    return {"ok": True}


@router.get("/providers", summary="List Chat Providers")
def list_providers():
    """Return available providers with models. Ollama models are discovered live."""
    from gitd.services.agent_chat import get_providers

    return get_providers()


@router.post("/warmup", summary="Pre-fill on-device KV cache")
def warmup_on_device(data: dict = Body({})):
    """
    Pre-bake the system + tool list prefix into the on-device LLM's KV cache
    so the user's first chat turn lands as a "turn 2" with full prefix
    reuse. Saves ~120 s of cold prefill on Snapdragon-class CPUs with the
    default 1.3K-token system prompt.

    The Android app should POST this once at launch (idempotent; subsequent
    calls are near-free if the prefix is already cached). No-op for
    non-llama.cpp runtimes.

    Body: {"device": "<adb_serial>", "model": "gemma-4-e2b-q4km-gguf"}
    """
    from gitd.services.agent_chat import DEFAULT_SYSTEM, system_prompt_for_device
    from gitd.services.agent_chat_ondevice import _kotlin_llm
    from gitd.services.agent_tools import tool_prompt_list, tools_for_device

    device = (data.get("device") or "").strip()
    model_id = (data.get("model") or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="model required")

    llm = _kotlin_llm()
    if llm is None:
        return {"ok": False, "warmed": 0, "reason": "Chaquopy bridge missing"}

    try:
        if not bool(llm.ensureLoaded(model_id)):
            return {"ok": False, "warmed": 0, "reason": f"model {model_id} not loaded"}
    except Exception as e:
        return {"ok": False, "warmed": 0, "reason": f"ensureLoaded failed: {e}"}

    # Stable prefix + disk cache path are computed in agent_chat_ondevice so
    # chat and warmup share the same key. Without this, the chat path's
    # generateStart sees an empty KV and pays a full cold prefill (~4 min on
    # Q5_K_M / Q6_K) every time even though the prefix is already on disk.
    from gitd.services.agent_chat_ondevice import (
        _ensure_kv_warmed,
        _kv_cache_path,
        ondevice_stable_prefix,
    )

    system = DEFAULT_SYSTEM.replace("{tool_list}", tool_prompt_list(tools_for_device(device)))
    system = system_prompt_for_device(device, system)
    stable_prefix = ondevice_stable_prefix(system, device)
    cache_path = _kv_cache_path(model_id, stable_prefix)

    # Try restore first.
    from_cache, loaded = _ensure_kv_warmed(llm, model_id, stable_prefix)
    if from_cache:
        return {"ok": True, "warmed": loaded, "model": model_id, "from_cache": True}

    # Cold path: do the full prefill.
    try:
        warmed = int(llm.warmup(model_id, stable_prefix))
    except Exception as e:
        return {"ok": False, "warmed": 0, "reason": f"warmup failed: {e}"}

    # Save for next launch (best-effort; failure doesn't break the warmup).
    try:
        getattr(llm, "saveState")(model_id, cache_path)
    except Exception:
        pass

    return {"ok": True, "warmed": warmed, "model": model_id, "from_cache": False}


# ── Ollama model management ──────────────────────────────────────────────

# model -> authoritative background pull state
_pull_status: dict[str, dict] = {}
_pull_status_lock = threading.Lock()


def _ollama_url(path: str = "") -> str:
    from ops.ai_settings import provider_runtime
    base_url = str(provider_runtime("ollama").get("base_url", "http://localhost:11434")).rstrip("/")
    return f"{base_url}{path}"


def _ollama_request(method: str, path: str, **kwargs):
    """Make one local Ollama request; callers decide how to expose errors."""
    import requests
    try:
        return requests.request(method, _ollama_url(path), **kwargs)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Ollama request failed ({type(exc).__name__})") from exc


def _json_response(response) -> dict:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


@router.get("/ollama/status", summary="Ollama Model Status")
def ollama_status():
    """Return live installed/loaded state and Ollama-advertised capabilities."""
    try:
        tags_response = _ollama_request("GET", "/api/tags", timeout=3)
        ps_response = _ollama_request("GET", "/api/ps", timeout=3)
    except Exception:
        return {"ok": False, "models": [], "error": "Ollama not reachable or request timed out", "base_url": _ollama_url()}
    if tags_response.status_code != 200 or ps_response.status_code != 200:
        return {"ok": False, "models": [], "error": "Ollama status request failed", "base_url": _ollama_url()}
    tags, ps = _json_response(tags_response), _json_response(ps_response)
    if not isinstance(tags.get("models"), list) or not isinstance(ps.get("models"), list):
        return {"ok": False, "models": [], "error": "Invalid Ollama status response"}
    loaded = {m.get("name"): m for m in ps.get("models", []) if m.get("name")}
    models = []
    for model in tags.get("models", []):
        name = model.get("name", "")
        if not name:
            continue
        show_caps = []
        try:
            show = _ollama_request("POST", "/api/show", json={"name": name}, timeout=5)
            if show.status_code == 200:
                raw = _json_response(show).get("capabilities", [])
                show_caps = [str(cap) for cap in raw if cap]
        except Exception:
            show_caps = []
        loaded_model = loaded.get(name)
        models.append({
            "name": name,
            "status": "loaded" if loaded_model else "downloaded",
            "vram_gb": round((loaded_model or {}).get("size_vram", 0) / 1e9, 1),
            "size_gb": round(model.get("size", 0) / 1e9, 1),
            "parameter_size": model.get("details", {}).get("parameter_size", ""),
            "capabilities": show_caps,
        })
    return {"ok": True, "models": models, "base_url": _ollama_url()}


@router.post("/ollama/pull", summary="Pull Ollama Model")
def ollama_pull(req: OllamaModelRequest):
    """Start a background streamed pull; response immediately reports pulling."""
    model = req.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="model required")
    with _pull_status_lock:
        current = _pull_status.get(model, {})
        if current.get("status") == "pulling":
            return {"ok": True, "status": "pulling", "model": model}
        job = {"status": "pulling", "model": model, "message": "starting"}
        _pull_status[model] = job  # reserve before another caller can start

    def publish(state):
        with _pull_status_lock:
            if _pull_status.get(model) is job:
                job.clear()
                job.update(state)

    def _do_pull():
        import requests
        response = None
        try:
            response = requests.post(_ollama_url("/api/pull"), json={"name": model, "stream": True},
                                     timeout=1800, stream=True)
            if response.status_code != 200:
                error = _json_response(response).get("error", getattr(response, "text", "pull failed")[:300])
                publish({"status": "error", "model": model, "error": error})
                return
            final = {"status": "pulling", "model": model}
            succeeded = False
            for raw in response.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                succeeded = item.get("status") == "success" or succeeded
                final["message"] = item.get("status", final.get("message", "pulling"))
                if item.get("total") is not None:
                    final["total"] = item.get("total")
                if item.get("completed") is not None:
                    final["completed"] = item.get("completed")
                if final.get("total"):
                    final["percent"] = round(float(final.get("completed", 0)) * 100 / float(final["total"]), 1)
                if item.get("error"):
                    final = {"status": "error", "model": model, "error": item["error"]}
                    break
                publish(final)
            if final.get("status") != "error":
                if succeeded:
                    final["status"] = "done"
                    final["percent"] = 100
                else:
                    final = {"status": "error", "model": model, "error": "Download stream ended before success"}
            publish(final)
        except Exception:
            publish({"status": "error", "model": model, "error": "Ollama pull failed"})
        finally:
            try:
                response.close()
            except Exception:
                pass

    try:
        threading.Thread(target=_do_pull, daemon=True, name=f"ollama-pull-{model}").start()
    except Exception:
        publish({"status": "error", "model": model, "error": "Ollama pull failed to start"})
        raise HTTPException(status_code=503, detail="Ollama pull failed to start") from None
    return {"ok": True, "status": "pulling", "model": model}


@router.get("/ollama/pull/{model:path}", summary="Check Pull Status")
def ollama_pull_status(model: str):
    with _pull_status_lock:
        state = dict(_pull_status.get(model, {}))
    if not state:
        return {"status": "unknown", "model": model}
    return {"model": model, **state}


@router.post("/ollama/load", summary="Load Ollama Model")
def ollama_load(req: OllamaModelRequest):
    try:
        response = _ollama_request("POST", "/api/generate", json={"model": req.model, "prompt": "", "keep_alive": "10m"}, timeout=120)
    except HTTPException as exc:
        return {"ok": False, "model": req.model, "error": exc.detail}
    data = _json_response(response)
    if response.status_code != 200:
        return {"ok": False, "model": req.model, "error": data.get("error", getattr(response, "text", "load failed")[:200])}
    return {"ok": True, "model": req.model}


@router.post("/ollama/unload", summary="Unload Ollama Model")
def ollama_unload(req: OllamaModelRequest):
    try:
        response = _ollama_request("POST", "/api/generate", json={"model": req.model, "keep_alive": 0}, timeout=10)
    except HTTPException as exc:
        return {"ok": False, "model": req.model, "error": exc.detail}
    if response.status_code != 200:
        data = _json_response(response)
        return {"ok": False, "model": req.model, "error": data.get("error", "unload failed")}
    return {"ok": True, "model": req.model}

