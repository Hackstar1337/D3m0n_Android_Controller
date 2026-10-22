"""Agent chat service — manages sessions, runs the agent loop with tool execution.

Supports multiple LLM providers:
  - claude-code: Free, local, uses `claude` CLI (default)
  - anthropic: Claude API with native tool_use
  - openrouter: Any model via OpenRouter
  - ollama: Local models
"""

import json
import logging
import os
import http.client
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from gitd.bots.common.device import is_ios_ref
from gitd.services.agent_tools import execute_tool, get_screenshot_b64, tool_prompt_list, tools_for_device
from gitd.services.device_context import get_phone_state, get_screen_tree
from gitd.services.llm_backoff import backoff_stream, effort_timeout

log = logging.getLogger(__name__)

DEFAULT_SYSTEM = """You are a mobile automation agent with full control over one connected mobile device.

You can see the screen (via screenshots and UI tree), interact with it (tap, swipe, type),
launch and control apps, navigate browser pages, and execute automation skills.

## Available tools:
{tool_list}

## How to use tools:
To call a tool, output a JSON block like this:
```tool
{{"tool": "tool_name", "args": {{"param": "value"}}}}
```

You can call multiple tools in sequence. After each tool call, I'll show you the result.

## Guidelines:
- Always use get_screen_tree first to understand what's on screen
- Use element indices from the tree for precise tapping (tap_element)
- After actions, verify results with get_screen_tree
- Use only the tools listed above; unavailable platform-specific tools have been filtered out
- Keep responses concise"""

ANTHROPIC_SYSTEM = """You are a mobile automation agent with full control over one connected mobile device.

You can see the screen (via screenshots and UI tree), interact with it (tap, swipe, type),
launch and control apps, navigate browser pages, and execute automation skills.

Guidelines:
- Always use get_screen_tree first to understand what's on screen before tapping
- Use element indices from the tree for precise tapping (tap_element)
- After performing actions, use get_screen_tree to verify the result
- Use only the tools exposed for the current device platform
- Keep responses concise — show what you did and the result"""

MAX_TURNS = 24
OLLAMA_DEFAULT_MODEL = "qwen3.5:4b"
OLLAMA_DEFAULT_CONTEXT = 8192
OLLAMA_DEFAULT_OUTPUT = 1024
OLLAMA_DEFAULT_TURNS = 12
MAX_CONTEXT_CHARS = 32768

PROVIDERS = {
    "claude-code": {"label": "Claude Code", "models": ["sonnet", "opus", "haiku"]},
    "anthropic": {"label": "Claude API", "models": ["claude-sonnet-4-20250514", "claude-opus-4-20250514"]},
    "openrouter": {"label": "OpenRouter", "models": ["anthropic/claude-sonnet-4", "google/gemini-2.5-pro"]},
    "ollama": {
        "label": "Ollama (local)",
        "models": [
            OLLAMA_DEFAULT_MODEL,
            "llama3.2:3b",
            "llama3.2:1b",
            "gemma3:4b",
            "qwen3:4b",
            "phi4-mini:3.8b",
            "mistral:7b",
        ],
    },
    # On-device — runs the model in-process via MediaPipe (.task) or
    # llama.cpp JNI (.gguf). The Kotlin OnDeviceModelRegistry is the source of
    # truth for ids; we ship a default subset here and overlay live ids below.
    "on-device": {
        "label": "On-device (Gemma)",
        "models": ["gemma-3-1b-it", "gemma-2-2b-it", "gemma-4-e2b-q4km-gguf"],
    },
    # vLLM — full-precision Gemma 4 served from the GPU box,
    # routed via Mac SSH tunnel + adb reverse so the phone hits it as if it
    # were on localhost. Same OpenAI-compatible shape as openrouter; we just
    # point the client at config.vllm_base_url instead.
    "vllm": {
        "label": "vLLM (remote GPU)",
        "models": [
            "unsloth/gemma-4-E2B-it",
            "unsloth/gemma-4-E2B-it-bnb-4bit",
            "unsloth/gemma-4-E4B-it",
            "unsloth/gemma-4-E4B-it-bnb-4bit",
        ],
    },
}


@dataclass
class ChatMessage:
    role: str
    content: str
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    tool_id: str = ""
    image_b64: str = ""


@dataclass
class ChatSession:
    id: str
    device: str
    provider: str = "claude-code"
    model: str = "sonnet"
    messages: list = field(default_factory=list)
    api_messages: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    auto_screenshot: bool = True
    context_length: int = OLLAMA_DEFAULT_CONTEXT
    max_output_tokens: int = OLLAMA_DEFAULT_OUTPUT
    max_turns: int = OLLAMA_DEFAULT_TURNS
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    current_run_id: str = ""
    cancelled_run_ids: set = field(default_factory=set, repr=False, compare=False)
    run_events: dict = field(default_factory=dict, repr=False, compare=False)
    run_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    run_state_lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)


_sessions: dict[str, ChatSession] = {}
_sessions_lock = threading.RLock()
_active_procs: dict[str, subprocess.Popen] = {}  # session_id -> running subprocess
_active_http_responses: dict[str, set] = {}  # session_id -> in-flight response objects
_active_http_lock = threading.Lock()
_ollama_worker_slots = threading.BoundedSemaphore(8)
_http_close_slots = threading.BoundedSemaphore(4)


class _OllamaRequestHandle:
    """Run-scoped connection owner, registered before any request dispatch."""

    def __init__(self, session_id: str, run_id: str, cancel_event: threading.Event):
        self.session_id = session_id
        self.run_id = run_id
        self.cancel_event = cancel_event  # never read a replacement run's event
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.response = None
        self.error = None
        self.connection = None
        self.sock = None
        self.lock = threading.Lock()

    def is_cancelled(self):
        return self.cancelled.is_set() or self.cancel_event.is_set()

    def attach_socket(self, sock):
        with self.lock:
            self.sock = sock
        if self.is_cancelled():
            self.abort()
            raise _OllamaRequestCancelled()

    def abort(self):
        self.cancelled.set()
        with self.lock:
            sock = self.sock or getattr(self.connection, "sock", None)
        if sock is not None:
            # close() alone may leave another thread's buffered recv blocked.
            # shutdown wakes that recv; the worker alone closes HTTPResponse.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def platform_context(device: str) -> str:
    if is_ios_ref(device):
        return (
            "Target platform: iOS via Appium/WebDriverAgent. Device refs look like ios:<udid>. "
            "Use iOS bundle ids with launch_app; call search_apps/list_apps if you need to discover a bundle id. "
            "Use browser tools such as open_url/extract_visible_text/extract_articles/read_news for web tasks, "
            "and avoid Android-only concepts such as ADB shell, Android intents, Portal overlay, Play Store, "
            "and Android package-manager commands."
        )
    return (
        "Target platform: Android via ADB/Portal. Device refs are Android serials. "
        "Android intents, ADB shell, app packages, Portal overlay, notifications, and package search "
        "may be available when their tools are listed."
    )


def system_prompt_for_device(device: str, base: str = ANTHROPIC_SYSTEM) -> str:
    return f"{base}\n\n{platform_context(device)}"


def openai_tools_for_device(device: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
        }
        for t in tools_for_device(device)
    ]


def _track_http_response(session_id: str, response, add: bool):
    with _active_http_lock:
        if add:
            _active_http_responses.setdefault(session_id, set()).add(response)
        else:
            bucket = _active_http_responses.get(session_id, set())
            bucket.discard(response)
            if not bucket:
                _active_http_responses.pop(session_id, None)


def _close_response_async(response):
    """Bound best-effort legacy response cleanup; native I/O closes in its worker."""
    if isinstance(response, _OllamaResponse):
        response.close()  # buffered/native close has no transport wait
        return
    if not _http_close_slots.acquire(blocking=False):
        return
    def close():
        try:
            response.close()
        except Exception:
            log.debug("provider response close failed", exc_info=True)
        finally:
            _http_close_slots.release()
    try:
        threading.Thread(target=close, daemon=True, name="agent-response-close").start()
    except Exception:
        _http_close_slots.release()
        log.debug("provider response close worker failed to start", exc_info=True)


def stop_agent(session_id: str, run_id: str = ""):
    """Signal only this session and asynchronously clean up its process/HTTP work."""
    with _sessions_lock:
        session = _sessions.get(session_id)
    target_run_id = run_id
    if session:
        with session.run_state_lock:
            target_run_id = run_id or session.current_run_id
            if target_run_id:
                session.cancelled_run_ids.add(target_run_id)
                # Bound stale/queued Stop tombstones per session.
                while len(session.cancelled_run_ids) > 128:
                    session.cancelled_run_ids.discard(next(i for i in session.cancelled_run_ids if i != target_run_id))
            if run_id:
                if run_id != session.current_run_id:
                    return  # queued/stale Stop cannot cancel a replacement
            event = session.run_events.get(target_run_id) if target_run_id else session.cancel_event
            (event or session.cancel_event).set()
            if target_run_id == session.current_run_id and session.run_lock.locked():
                session.run_lock.release()
    with _active_http_lock:
        responses = list(_active_http_responses.get(session_id, ()))
    for response in responses:
        if target_run_id and getattr(response, "run_id", target_run_id) != target_run_id:
            continue
        if hasattr(response, "abort"):
            response.abort()
        else:
            _close_response_async(response)

    proc = _active_procs.pop(session_id, None) if not run_id else None
    if proc is None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, getattr(__import__('signal'), 'SIGTERM'))
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        pass
    log.info("Stopped agent for session %s", session_id)


def begin_agent_run(session: ChatSession, run_id: str = ""):
    """Acquire the per-session run guard; no concurrent turns for one session."""
    with session.run_state_lock:
        if not session.run_lock.acquire(blocking=False):
            raise RuntimeError("A message is already running for this session")
        if run_id and run_id in session.cancelled_run_ids:
            session.run_lock.release()
            raise RuntimeError("This message was stopped before it started")
        session.current_run_id = run_id or uuid.uuid4().hex
        session.cancel_event = threading.Event()
        # Older workers retain their own event; no unbounded run-event registry.
        session.run_events.clear()
        session.run_events[session.current_run_id] = session.cancel_event


def end_agent_run(session: ChatSession, run_id: str = ""):
    with session.run_state_lock:
        run_id = run_id or session.current_run_id
        session.run_events.pop(run_id, None)
        if run_id == session.current_run_id and session.run_lock.locked():
            session.run_lock.release()


def _cancelled(session: ChatSession, run_id: str = "") -> bool:
    with session.run_state_lock:
        if run_id:
            event = session.run_events.get(run_id)
            return session.current_run_id != run_id or event is None or event.is_set()
        return session.cancel_event.is_set()


def _run_is_current(session: ChatSession, run_id: str) -> bool:
    with session.run_state_lock:
        return bool(run_id) and session.current_run_id == run_id and run_id in session.run_events


def create_session(device: str, provider: str = "", model: str = "", system_prompt: str = "") -> ChatSession:
    sid = str(uuid.uuid4())[:8]
    configured_model = ""
    configured_defaults = {}
    if not provider:
        # Read the encrypted default per new session; existing sessions are not
        # mutated when the dashboard default changes.
        try:
            from ops.ai_settings import load_settings
            selected = load_settings()
            provider = selected["default_provider"]
            configured_model = selected.get("default_model", "")
            configured_defaults = dict(selected.get("defaults", {}))
        except Exception:
            raise RuntimeError("AI settings could not be loaded; no provider was selected") from None
    if not configured_model:
        try:
            from ops.ai_settings import provider_runtime
            configured_model = provider_runtime(provider).get("default_model", "")
        except Exception:
            configured_model = ""
    default_model = configured_model or PROVIDERS.get(provider, {}).get("models", ["sonnet"])[0] if not model else model
    session = ChatSession(
        id=sid, device=device, provider=provider, model=default_model or "sonnet",
        context_length=int(configured_defaults.get("context_length", OLLAMA_DEFAULT_CONTEXT)),
        max_output_tokens=int(configured_defaults.get("max_output_tokens", OLLAMA_DEFAULT_OUTPUT)),
        max_turns=int(configured_defaults.get("max_turns", OLLAMA_DEFAULT_TURNS)),
    )
    with _sessions_lock:
        _sessions[sid] = session
    return session


def get_session(sid: str) -> ChatSession | None:
    with _sessions_lock:
        return _sessions.get(sid)


def snapshot_sessions() -> list[dict]:
    """Return a concurrency-safe, metadata-only view of active chat sessions.

    The run lock and cancellation event are the sole source of truth for live
    activity.  Copying the session objects while holding the registry lock
    prevents a concurrent create/delete from invalidating the iteration; the
    objects remain valid after the lock is released for the lightweight state
    read below.
    """
    with _sessions_lock:
        sessions = tuple(_sessions.values())
    rows = []
    for session in sessions:
        locked = session.run_lock.locked()
        cancelled = session.cancel_event.is_set()
        rows.append(
            {
                "id": session.id,
                "device": session.device,
                "provider": session.provider,
                "model": session.model,
                "messages": len(session.messages),
                "running": locked and not cancelled,
                "stopping": locked and cancelled,
            }
        )
    return rows


def list_sessions() -> list[dict]:
    return snapshot_sessions()


def delete_session(sid: str):
    stop_agent(sid)
    with _sessions_lock:
        _sessions.pop(sid, None)


# ── Persistence (DB) ───────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_session_to_db(session: ChatSession):
    """Persist a ChatSession to the database (upsert conversation + append new messages)."""
    from gitd.models.base import SessionLocal
    from gitd.models.chat import ChatConversation, ChatMessageRow

    db = SessionLocal()
    try:
        conv = db.query(ChatConversation).filter_by(id=session.id).first()
        now = _utcnow_iso()

        if not conv:
            # Auto-generate title from first user message
            title = ""
            for msg in session.messages:
                if msg.role == "user" and msg.content:
                    title = msg.content[:50]
                    break
            conv = ChatConversation(
                id=session.id,
                device=session.device,
                provider=session.provider,
                model=session.model,
                title=title,
                created_at=now,
                updated_at=now,
                message_count=0,
            )
            db.add(conv)

        conv.updated_at = now
        conv.message_count = len(session.messages)

        # Only insert messages that haven't been saved yet
        existing_count = db.query(ChatMessageRow).filter_by(conversation_id=session.id).count()
        for msg in session.messages[existing_count:]:
            db.add(
                ChatMessageRow(
                    conversation_id=session.id,
                    role=msg.role,
                    content=msg.content or "",
                    tool_name=msg.tool_name or "",
                    tool_args=json.dumps(msg.tool_args) if msg.tool_args else "{}",
                    tool_id=msg.tool_id or "",
                    created_at=now,
                )
            )

        db.commit()
    except Exception:
        db.rollback()
        log.exception("Failed to save session %s to DB", session.id)
    finally:
        db.close()


def list_conversations(device: str | None = None) -> list[dict]:
    """Return saved conversations, newest first."""
    from gitd.models.base import SessionLocal
    from gitd.models.chat import ChatConversation

    db = SessionLocal()
    try:
        q = db.query(ChatConversation)
        if device:
            q = q.filter_by(device=device)
        rows = q.order_by(ChatConversation.updated_at.desc()).all()
        return [
            {
                "id": r.id,
                "device": r.device,
                "provider": r.provider,
                "model": r.model,
                "title": r.title,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                "message_count": r.message_count,
            }
            for r in rows
        ]
    finally:
        db.close()


def load_conversation(conversation_id: str) -> ChatSession | None:
    """Load a conversation from DB into an active ChatSession (placed in _sessions)."""
    from gitd.models.base import SessionLocal
    from gitd.models.chat import ChatConversation, ChatMessageRow

    db = SessionLocal()
    try:
        conv = db.query(ChatConversation).filter_by(id=conversation_id).first()
        if not conv:
            return None

        rows = db.query(ChatMessageRow).filter_by(conversation_id=conversation_id).order_by(ChatMessageRow.id).all()

        messages = []
        api_messages = []
        for r in rows:
            tool_args = {}
            if r.tool_args and r.tool_args != "{}":
                try:
                    tool_args = json.loads(r.tool_args)
                except (json.JSONDecodeError, TypeError):
                    pass
            messages.append(
                ChatMessage(
                    role=r.role,
                    content=r.content or "",
                    tool_name=r.tool_name or "",
                    tool_args=tool_args,
                    tool_id=r.tool_id or "",
                    image_b64="",
                )
            )

            # Rebuild api_messages for anthropic provider
            if conv.provider == "anthropic":
                if r.role == "user":
                    api_messages.append({"role": "user", "content": r.content or ""})
                elif r.role == "assistant":
                    api_messages.append({"role": "assistant", "content": r.content or ""})
                # tool_call and tool_result are harder to reconstruct exactly,
                # so for anthropic the resumed session may lose tool history.
                # Claude-code is stateless per turn so this is fine.

        session = ChatSession(
            id=conv.id,
            device=conv.device,
            provider=conv.provider,
            model=conv.model,
            messages=messages,
            api_messages=api_messages,
        )
        with _sessions_lock:
            _sessions[conv.id] = session
        return session
    finally:
        db.close()


def delete_conversation(conversation_id: str):
    """Delete a conversation and its messages from the database."""
    from gitd.models.base import SessionLocal
    from gitd.models.chat import ChatConversation, ChatMessageRow

    db = SessionLocal()
    try:
        db.query(ChatMessageRow).filter_by(conversation_id=conversation_id).delete()
        db.query(ChatConversation).filter_by(id=conversation_id).delete()
        db.commit()
    except Exception:
        db.rollback()
        log.exception("Failed to delete conversation %s", conversation_id)
    finally:
        db.close()
    # Also remove from in-memory sessions if present
    with _sessions_lock:
        _sessions.pop(conversation_id, None)


def get_providers() -> list[dict]:
    """Return provider metadata; Ollama discovery is best-effort and never replaces others."""
    import requests
    from ops.ai_settings import SettingsError, provider_runtime

    try:
        runtimes = {pid: provider_runtime(pid) for pid in PROVIDERS}
    except SettingsError:
        return [
            {
                "id": pid,
                "label": info["label"],
                "models": [],
                "ready": False,
                "error": "AI settings storage unavailable",
            }
            for pid, info in PROVIDERS.items()
        ]
    result = []
    for pid, info in PROVIDERS.items():
        try:
            runtime = runtimes[pid]
            models = list(runtime.get("models") or info["models"])
            base_url = str(runtime.get("base_url", "")).rstrip("/")
        except Exception:
            runtime, models, base_url = {}, list(info["models"]), ""
        metadata = {}
        if pid == "ollama":
            try:
                r = requests.get(f"{base_url}/api/tags", timeout=3)
                if r.status_code == 200:
                    installed = [m.get("name") for m in r.json().get("models", []) if m.get("name")]
                    if installed:
                        models = list(dict.fromkeys(models + installed))
                    metadata = {"ready": True, "installed": installed}
            except Exception as exc:
                metadata = {"ready": False, "error": "Ollama discovery unavailable"}
        result.append({"id": pid, "label": info["label"], "models": models, **metadata})
    return result


def chat_turn(session: ChatSession, user_message: str, *, context_length: int | None = None,
              max_output_tokens: int | None = None, max_turns: int | None = None, _reserved: bool = False,
              _run_id: str = ""):
    """Run one guarded agent turn. Provider-specific loops remain isolated."""
    if not _reserved:
        begin_agent_run(session)
    run_id = (_run_id or session.current_run_id) if _reserved else session.current_run_id
    if _cancelled(session, run_id):
        yield {"type": "stopped", "content": "Agent stopped before starting"}
        yield {"type": "done"}
        return
    if context_length is not None:
        session.context_length = max(1024, min(int(context_length), 131072))
    if max_output_tokens is not None:
        session.max_output_tokens = max(64, min(int(max_output_tokens), 16384))
    if max_turns is not None:
        session.max_turns = max(1, min(int(max_turns), 24))
    try:
        if _cancelled(session, run_id):
            yield {"type": "stopped", "content": "Agent stopped before starting"}
            yield {"type": "done"}
            return
        provider = session.provider
        if provider == "anthropic":
            yield from _chat_anthropic(session, user_message, run_id=run_id)
        elif provider == "claude-code":
            from gitd.services.agent_chat_claude_code import chat_claude_code
            yield from chat_claude_code(session, user_message)
        elif provider == "openrouter":
            yield from _chat_openrouter(session, user_message, run_id=run_id)
        elif provider == "vllm":
            yield from _chat_vllm(session, user_message, run_id=run_id)
        elif provider == "ollama":
            yield from _chat_ollama(session, user_message, run_id=run_id)
        elif provider == "on-device":
            from gitd.services.agent_chat_ondevice import chat_ondevice
            yield from chat_ondevice(session, user_message)
        else:
            yield {"type": "error", "content": f"Unknown provider: {provider}"}
    finally:
        if not _reserved:
            end_agent_run(session, run_id=run_id)

_META_TOOLS = {"draft_skill", "save_skill"}


def maybe_handle_meta_tool(session: ChatSession, name: str, args: dict) -> str | None:
    """Handle conversation-level meta tools (draft_skill / save_skill).

    These operate on the whole session (not the device UI), so they run here —
    where `session` is in scope — instead of the stateless execute_tool. Returns
    the result string, or None for a normal device tool so the caller falls back
    to execute_tool. The session is flushed to the DB first so the distiller
    reads a complete, ordered action trace.
    """
    if name not in _META_TOOLS:
        return None
    import json as _json

    try:
        save_session_to_db(session)
    except Exception:
        log.exception("meta-tool %s: failed to flush session %s", name, session.id)
    try:
        if name == "draft_skill":
            from gitd.services.skills_from_chat import draft_hard_skill

            return _json.dumps(draft_hard_skill(session.id), indent=2)
        # save_skill
        from gitd.services.skills_from_chat import commit_skill

        res = commit_skill(
            kind=args.get("kind", "hard"),
            name=(args.get("name") or "").strip().lower().replace(" ", "_"),
            app_package=args.get("app_package", ""),
            description=args.get("description", ""),
            steps=args.get("steps"),
            guidance=args.get("guidance"),
            conversation_id=session.id,
        )
        return _json.dumps(res, indent=2)
    except Exception as e:
        return f"Error: {e}"


def _dispatch_tool(session: ChatSession, name: str, args: dict, run_id: str = "") -> str:
    """Dispatch a tool only for the session device, and never after Stop."""
    args = dict(args or {})
    requested = args.get("device")
    if requested and requested != session.device:
        raise ValueError("Tool device must match the session device")
    if _cancelled(session, run_id):
        raise RuntimeError("Agent stopped; no further device action permitted")
    allowed = {tool["name"] for tool in tools_for_device(session.device)}
    if name not in allowed:
        raise ValueError("Tool is unavailable for the session device")
    args["device"] = session.device
    result = maybe_handle_meta_tool(session, name, args)
    return result if result is not None else execute_tool(name, args)


# ── Anthropic API (native tool_use) ──────────────────────────────────────────


def _append_run_message(session, run_id, message):
    """Atomic old-run commit boundary; late callbacks cannot write a new run."""
    with session.run_state_lock:
        if _cancelled(session, run_id):
            return False
        session.messages.append(message)
        return True


def _append_run_api_message(session, run_id, message):
    with session.run_state_lock:
        if _cancelled(session, run_id):
            return False
        session.api_messages.append(message)
        return True


class _AgentRunCancelled(RuntimeError):
    pass


def _provider_backoff(session, run_id, fn):
    """Guard every retry, and interrupt rate-limit waits with the OLD event."""
    with session.run_state_lock:
        event = session.run_events.get(run_id, session.cancel_event)
    def call():
        if event.is_set() or _cancelled(session, run_id):
            raise _AgentRunCancelled("Agent stopped")
        return fn()
    for ev in backoff_stream(call, sleep=event.wait):
        if event.is_set() or _cancelled(session, run_id):
            raise _AgentRunCancelled("Agent stopped")
        yield ev


def _chat_anthropic(session: ChatSession, user_message: str, run_id: str = ""):
    """Use Anthropic API with native tool calling."""
    import anthropic

    run_id = run_id or session.current_run_id
    if _cancelled(session, run_id):
        yield {"type": "stopped", "content": "Agent stopped before starting"}
        yield {"type": "done"}
        return
    _append_run_message(session, run_id, ChatMessage(role="user", content=user_message))
    _append_run_api_message(session, run_id, {"role": "user", "content": _build_vision_content(session, user_message)})

    from ops.ai_settings import provider_runtime
    runtime = provider_runtime("anthropic")
    client = anthropic.Anthropic(api_key=runtime.get("api_key", ""), base_url=runtime.get("base_url", "https://api.anthropic.com"), max_retries=0)
    available_tools = tools_for_device(session.device)

    run_id = run_id or session.current_run_id
    for turn in range(MAX_TURNS):
        if _cancelled(session, run_id):
            yield {"type": "stopped", "content": "Agent stopped"}
            yield {"type": "done"}
            return
        try:
            resp = None
            for ev in _provider_backoff(session, run_id,
                lambda: client.messages.create(
                    model=session.model,
                    max_tokens=4096,
                    system=system_prompt_for_device(session.device),
                    messages=session.api_messages,
                    tools=available_tools,
                    timeout=effort_timeout(session.model),
                )
            ):
                if "__result__" in ev:
                    resp = ev["__result__"]
                else:
                    yield ev
        except _AgentRunCancelled:
            yield {"type": "stopped", "content": "Agent stopped"}
            yield {"type": "done"}
            return
        except Exception:
            yield {"type": "error", "content": "Anthropic request failed"}
            return

        if _cancelled(session, run_id):
            yield {"type": "stopped", "content": "Agent stopped before processing the model reply"}
            yield {"type": "done"}
            return

        has_tool_use = False
        tool_results = []

        for block in resp.content:
            if _cancelled(session, run_id):
                yield {"type": "stopped", "content": "Agent stopped before processing the model reply"}
                yield {"type": "done"}
                return
            if block.type == "text":
                _append_run_message(session, run_id, ChatMessage(role="assistant", content=block.text))
                yield {"type": "text", "content": block.text}
            elif block.type == "tool_use":
                has_tool_use = True
                tool_name = block.name
                tool_args = dict(block.input)
                tool_args.setdefault("device", session.device)

                _append_run_message(session, run_id, 
                    ChatMessage(
                        role="tool_call", tool_name=tool_name, tool_args=tool_args, tool_id=block.id, content=""
                    )
                )
                yield {"type": "tool_call", "name": tool_name, "args": tool_args}

                # Catch tool failures (e.g. ADBError on an offline/unauthorized
                # device) so they become a tool_result the model can react to,
                # not an uncaught raise that breaks the SSE stream. Every
                # tool_use MUST get a matching tool_result or the next turn's
                # Anthropic request is malformed — so the error text flows into
                # the same result path below. Mirrors the vllm/ollama loops.
                try:
                    result = _dispatch_tool(session, tool_name, tool_args, run_id)
                except Exception as e:
                    result = f"Tool error: {e}"
                if _cancelled(session, run_id):
                    yield {"type": "stopped", "content": "Agent stopped after device action"}
                    yield {"type": "done"}
                    return
                image_b64 = ""
                if tool_name in ("screenshot", "screenshot_annotated", "screenshot_cropped"):
                    image_b64 = get_screenshot_b64(tool_args.get("device", session.device)) or ""

                _append_run_message(session, run_id, 
                    ChatMessage(
                        role="tool_result", content=result, tool_name=tool_name, tool_id=block.id, image_b64=image_b64
                    )
                )
                yield {"type": "tool_result", "name": tool_name, "result": result[:500]}
                if image_b64:
                    yield {"type": "screenshot", "image": image_b64}

                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result[:5000]})

        _append_run_api_message(session, run_id, {"role": "assistant", "content": resp.content})
        if has_tool_use:
            _append_run_api_message(session, run_id, {"role": "user", "content": tool_results})
        else:
            break

    yield {"type": "done"}


# ── OpenRouter ───────────────────────────────────────────────────────────────


def _chat_openrouter(session: ChatSession, user_message: str, run_id: str = ""):
    """Use OpenRouter with OpenAI-compatible tool calling.

    Multi-turn agent loop (same shape as _chat_vllm): feed each tool result
    back to the model until it answers without tool calls or MAX_TURNS.
    """
    from openai import OpenAI

    run_id = run_id or session.current_run_id
    if _cancelled(session, run_id):
        yield {"type": "stopped", "content": "Agent stopped before starting"}
        yield {"type": "done"}
        return
    _append_run_message(session, run_id, ChatMessage(role="user", content=user_message))

    from ops.ai_settings import provider_runtime
    runtime = provider_runtime("openrouter")
    client = OpenAI(
        api_key=runtime.get("api_key", ""),
        base_url=runtime.get("base_url", "https://openrouter.ai/api/v1"),
        max_retries=0,  # retries must pass the captured run cancellation guard
    )

    oai_tools = openai_tools_for_device(session.device)

    context = ""
    try:
        tree = get_screen_tree(session.device)
        state = get_phone_state(session.device)
        context = f"[Screen]\n{tree[:1500]}\n[App: {state.get('currentApp', '?')}]\n\n"
    except Exception:
        pass

    messages: list[dict] = [
        {"role": "system", "content": system_prompt_for_device(session.device)},
        {"role": "user", "content": f"{context}Device: {session.device}\n\n{user_message}"},
    ]

    model = session.model or "anthropic/claude-sonnet-4"

    run_id = run_id or session.current_run_id
    for _turn in range(MAX_TURNS):
        if _cancelled(session, run_id):
            yield {"type": "stopped", "content": "Agent stopped"}
            yield {"type": "done"}
            return
        try:
            resp = None
            for ev in _provider_backoff(session, run_id,
                lambda: client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=oai_tools,
                    max_tokens=4096,
                    timeout=effort_timeout(model),
                )
            ):
                if "__result__" in ev:
                    resp = ev["__result__"]
                else:
                    yield ev
        except _AgentRunCancelled:
            yield {"type": "stopped", "content": "Agent stopped"}
            yield {"type": "done"}
            return
        except Exception:
            yield {"type": "error", "content": "OpenRouter request failed"}
            yield {"type": "done"}
            return

        if _cancelled(session, run_id):
            yield {"type": "stopped", "content": "Agent stopped before processing the model reply"}
            yield {"type": "done"}
            return
        msg = resp.choices[0].message

        if msg.content:
            _append_run_message(session, run_id, ChatMessage(role="assistant", content=msg.content))
            yield {"type": "text", "content": msg.content}

        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            break

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ],
            }
        )

        for tc in tool_calls:
            if _cancelled(session, run_id):
                yield {"type": "stopped", "content": "Agent stopped before device action"}
                yield {"type": "done"}
                return
            tool_name = tc.function.name
            try:
                tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                tool_args = {}
            tool_args.setdefault("device", session.device)

            _append_run_message(session, run_id, ChatMessage(role="tool_call", tool_name=tool_name, tool_args=tool_args, content=""))
            yield {"type": "tool_call", "name": tool_name, "args": tool_args}

            try:
                result = _dispatch_tool(session, tool_name, tool_args, run_id)
            except Exception as e:
                result = f"Tool error: {e}"
            if _cancelled(session, run_id):
                yield {"type": "stopped", "content": "Agent stopped after device action"}
                yield {"type": "done"}
                return
            _append_run_message(session, run_id, ChatMessage(role="tool_result", content=result[:500], tool_name=tool_name))
            yield {"type": "tool_result", "name": tool_name, "result": result[:500]}

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tool_name,
                    "content": result[:1500],
                }
            )

    yield {"type": "done"}


# ── vLLM (OpenAI-compatible, remote GPU via SSH) ──────────────────────────────


def _chat_vllm(session: ChatSession, user_message: str, run_id: str = ""):
    """Use a vLLM server (default: remote GPU via SSH tunnel + adb reverse) with
    OpenAI-compatible tool calling and multi-turn agent loop.

    Same OpenAI surface as _chat_openrouter, but unlike that one we DO loop on
    tool results — the whole point of routing to a real GPU is to put a smarter
    model into the actual agent loop (open Settings → tap Wi-Fi → ...), not
    just emit a single round of tool calls.
    """
    from openai import OpenAI

    run_id = run_id or session.current_run_id
    if _cancelled(session, run_id):
        yield {"type": "stopped", "content": "Agent stopped before starting"}
        yield {"type": "done"}
        return
    from ops.ai_settings import provider_runtime
    runtime = provider_runtime("vllm")
    _append_run_message(session, run_id, ChatMessage(role="user", content=user_message))

    client = OpenAI(
        api_key=runtime.get("api_key", "") or "EMPTY",
        base_url=runtime.get("base_url", "http://127.0.0.1:8000/v1"),
        max_retries=0,
    )

    oai_tools = openai_tools_for_device(session.device)

    # Initial screen context.
    context = ""
    try:
        tree = get_screen_tree(session.device)
        state = get_phone_state(session.device)
        context = f"[Screen]\n{tree[:1500]}\n[App: {state.get('currentApp', '?')}]\n\n"
    except Exception:
        pass

    messages: list[dict] = [
        {"role": "system", "content": system_prompt_for_device(session.device)},
        {"role": "user", "content": f"{context}Device: {session.device}\n\n{user_message}"},
    ]

    model = session.model or "unsloth/gemma-4-E4B-it"

    run_id = run_id or session.current_run_id
    for turn in range(MAX_TURNS):
        if _cancelled(session, run_id):
            yield {"type": "stopped", "content": "Agent stopped"}
            yield {"type": "done"}
            return
        yield {"type": "activity", "content": f"🧠 Inferring (turn {turn + 1})..."}

        try:
            resp = None
            for ev in _provider_backoff(session, run_id,
                lambda: client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=oai_tools,
                    max_tokens=2048,
                    timeout=effort_timeout(model),
                )
            ):
                if "__result__" in ev:
                    resp = ev["__result__"]
                else:
                    yield ev
        except _AgentRunCancelled:
            yield {"type": "stopped", "content": "Agent stopped"}
            yield {"type": "done"}
            return
        except Exception:
            yield {
                "type": "error",
                "content": "vLLM request failed; verify the configured private endpoint",
            }
            yield {"type": "done"}
            return

        if _cancelled(session, run_id):
            yield {"type": "stopped", "content": "Agent stopped before processing the model reply"}
            yield {"type": "done"}
            return
        msg = resp.choices[0].message

        if msg.content:
            _append_run_message(session, run_id, ChatMessage(role="assistant", content=msg.content))
            yield {"type": "text", "content": msg.content}

        # OpenAI-shape tool_calls. If absent, the model is done.
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            break

        # Append the assistant turn to the rolling conversation BEFORE running
        # tools so the next request sees the assistant's tool_calls in
        # context (OpenAI shape requires this).
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ],
            }
        )

        for tc in tool_calls:
            if _cancelled(session, run_id):
                yield {"type": "stopped", "content": "Agent stopped before device action"}
                yield {"type": "done"}
                return
            tool_name = tc.function.name
            try:
                tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                tool_args = {}
            tool_args.setdefault("device", session.device)

            _append_run_message(session, run_id, ChatMessage(role="tool_call", tool_name=tool_name, tool_args=tool_args, content=""))
            yield {"type": "tool_call", "name": tool_name, "args": tool_args}

            try:
                result = _dispatch_tool(session, tool_name, tool_args, run_id)
            except Exception as e:
                result = f"Tool error: {e}"
            if _cancelled(session, run_id):
                yield {"type": "stopped", "content": "Agent stopped after device action"}
                yield {"type": "done"}
                return
            _append_run_message(session, run_id, ChatMessage(role="tool_result", content=result[:500], tool_name=tool_name))
            yield {"type": "tool_result", "name": tool_name, "result": result[:500]}

            # Feed result back to the model for the next turn.
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tool_name,
                    "content": result[:1500],
                }
            )

    yield {"type": "done"}


# ── Tool-call parsing + Ollama ────────────────────────────────────────────────


def _parse_tool_calls(text: str) -> list[dict]:
    """Extract tool calls from LLM output.

    Handles a fair amount of slop because small models — especially raw Gemma 4 —
    emit f-string-style doubled braces, half-quoted keys, trailing ``, " "``
    junk, missing/extra closing braces, and similar near-misses.

    Accepted shapes:
      - {"tool": "X", "args": {...}}           (canonical)
      - {"tool": "X", "kwarg1": ..., ...}      (flat — e.g. ghost-gemma trained)
      - {"action_type": "X", ...}              (action-schema — translated to tool)
    """
    import re

    if not text:
        return []

    calls: list[dict] = []

    # Map from action-schema "action_type" → ("tool", arg-key-rewrites). For raw
    # Gemma 4 emitting the action schema we trained on, this lets the dispatcher
    # see canonical tool calls without retraining the parser side.
    action_to_tool = {
        "open_app": ("launch_app", {"app_name": "package"}),
        "click": ("tap", {"x": "x", "y": "y"}),
        "tap": ("tap", {}),
        "long_press": ("long_press", {}),
        "type_text": ("input_text", {"text": "text"}),
        "input_text": ("input_text", {}),
        "swipe": ("swipe", {}),
        "key_event": ("key_event", {"key": "key"}),
        "screenshot": ("screenshot", {}),
        "wait": ("wait", {"duration_ms": "ms"}),
        "force_stop": ("force_stop", {}),
    }

    def _coerce_action(d: dict) -> dict | None:
        action = d.get("action_type")
        if not isinstance(action, str):
            return None
        mapping = action_to_tool.get(action)
        if not mapping:
            return None
        tool_name, key_map = mapping
        args = {}
        for k, v in d.items():
            if k == "action_type":
                continue
            args[key_map.get(k, k)] = v
        return {"tool": tool_name, "args": args}

    def _try_dict(d: object) -> bool:
        if not isinstance(d, dict):
            return False
        if "tool" in d:
            calls.append(d)
            return True
        coerced = _coerce_action(d)
        if coerced:
            calls.append(coerced)
            return True
        return False

    def _try_loads(raw: str) -> bool:
        try:
            return _try_dict(json.loads(raw))
        except (ValueError, TypeError):
            return False

    def _attempt_repairs(raw: str) -> bool:
        """Run a chain of cleanups, retrying json.loads at every checkpoint."""
        candidate = raw

        # Doubled braces (Gemma f-string artefact) → singles. Only run when at
        # least one ``{{`` is present — otherwise we'd corrupt valid JSON like
        # ``{"a":{"b":1}}`` which has trailing ``}}`` for nested closes.
        # Do it ONCE only; iterating collapses legitimate triples like ``}}}``
        # (which is ``}}`` + ``}`` in the doubled convention) past the right
        # shape.
        if "{{" in candidate:
            new = candidate.replace("{{", "{").replace("}}", "}")
            if new != candidate:
                candidate = new
                if _try_loads(candidate):
                    return True

        # Strip dangling-comma "junk pairs" like ``, " "`` or ``, ""`` that some
        # models tack on before a closing brace.
        cleaned = re.sub(r',\s*"[^"]*"\s*(?=[,}])', "", candidate)
        if cleaned != candidate:
            candidate = cleaned
            if _try_loads(candidate):
                return True

        # Drop trailing ``,`` before ``}`` / ``]``.
        cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
        if cleaned != candidate:
            candidate = cleaned
            if _try_loads(candidate):
                return True

        # Truncate to the first balanced brace span — handles trailing prose
        # or extra closing braces.
        depth = 0
        start = candidate.find("{")
        if start >= 0:
            for i in range(start, len(candidate)):
                ch = candidate[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        if _try_loads(candidate[start : i + 1]):
                            return True
                        break

        return False

    # 1) ```tool / ```json fenced blocks (prompt asks for these)
    for match in re.finditer(r"```(?:tool|json)?\s*\n?(.*?)\n?```", text, re.DOTALL):
        raw = match.group(1).strip()
        if not raw:
            continue
        if _try_loads(raw):
            continue
        _attempt_repairs(raw)

    if calls:
        return calls

    # 2) No fences — scan for inline JSON objects mentioning "tool" or
    #    "action_type". Greedy: match every {...} and try each.
    for match in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL):
        raw = match.group(0)
        if '"tool"' not in raw and '"action_type"' not in raw:
            continue
        if _try_loads(raw):
            continue
        _attempt_repairs(raw)

    if calls:
        return calls

    # 3) Last-ditch fallback — Gemma at temp 0 routinely emits inline doubled
    #    braces with a mismatched count of closing `}` (e.g. five `}` for two
    #    `{{`). The step-2 regex above can't match a `{{` start because it
    #    expects a non-brace character after the first `{`. Collapse doubled
    #    braces over the whole text and try the same scan again.
    if "{{" in text or "}}" in text:
        flattened = text.replace("{{", "{").replace("}}", "}")
        for match in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", flattened, re.DOTALL):
            raw = match.group(0)
            if '"tool"' not in raw and '"action_type"' not in raw:
                continue
            if _try_loads(raw):
                continue
            _attempt_repairs(raw)

    return calls


def normalize_tool_call(call: dict) -> tuple[str, dict]:
    """Split a parsed tool call into ``(tool_name, args)``.

    Two shapes are seen in the wild and every provider must accept both:
      - ``{"tool": "X", "args": {...}}``          (gemma-4-e2b, llama, canonical)
      - ``{"tool": "X", "package": "...", ...}``   (ghost-gemma trained, qwen — flat)

    Prefer the nested ``args`` dict; otherwise treat the rest of the dict
    (every key except ``tool``) as kwargs. Returns a fresh dict the caller
    can mutate (e.g. ``setdefault("device", ...)``) without touching ``call``.
    """
    tool_name = call.get("tool", "")
    raw_args = call.get("args")
    if isinstance(raw_args, dict):
        args = dict(raw_args)
    else:
        args = {k: v for k, v in call.items() if k != "tool"}
    return tool_name, args


def _ollama_base_url() -> str:
    from ops.ai_settings import provider_runtime
    return str(provider_runtime("ollama").get("base_url", "http://localhost:11434")).rstrip('/')


class _OllamaRequestCancelled(RuntimeError):
    """The run was stopped while an Ollama request was pending."""


class _OllamaHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        self._agent_handle.attach_socket(self.sock)


class _OllamaHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Publish the TCP socket before the TLS handshake, then publish the
        # wrapped socket before handshake reads. Certificate checks stay on.
        http.client.HTTPConnection.connect(self)
        self._agent_handle.attach_socket(self.sock)
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname,
                                             do_handshake_on_connect=False)
        self._agent_handle.attach_socket(self.sock)
        self.sock.do_handshake()


class _OllamaResponse:
    """Buffered response: provider reads finish in the cancellable worker."""
    def __init__(self, status: int, body: bytes, headers):
        self.status_code = status
        self.content = body
        self.headers = dict(headers)
        self.text = body.decode("utf-8", errors="replace")
        self.closed = False

    def json(self):
        return json.loads(self.content)

    def close(self):
        self.closed = True  # all transport resources already closed by worker


def _ollama_http_request(handle, method: str, url: str, **kwargs):
    """Native inference transport seam; no requests adapter hides blocked recv.

    A connection is registered before connect/send/getresponse/read. Stop uses
    shutdown(SHUT_RDWR) and close on its socket, including during body reads.
    OS DNS/connect cannot always be interrupted; finite timeout and the global
    worker cap bound that residual without pretending to stop server compute.
    """
    import requests

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Ollama requires an HTTP or HTTPS endpoint")
    timeout = kwargs.pop("timeout", 120)
    if isinstance(timeout, tuple):
        connect_timeout, read_timeout = timeout
    else:
        connect_timeout = read_timeout = timeout
    connect_timeout = float(connect_timeout or 120)
    read_timeout = float(read_timeout or 120)
    headers = {"Accept": "application/json", "Connection": "close", **kwargs.pop("headers", {})}
    body = None
    if "json" in kwargs:
        body = json.dumps(kwargs.pop("json"), ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    kwargs.pop("stream", None)  # HTTP streaming flag is internal, payload stays stream:false
    if kwargs:
        raise TypeError(f"Unsupported Ollama transport options: {', '.join(kwargs)}")
    connection_type = _OllamaHTTPSConnection if parts.scheme == "https" else _OllamaHTTPConnection
    connection = connection_type(parts.hostname, port=parts.port, timeout=connect_timeout)
    connection._agent_handle = handle
    with handle.lock:
        handle.connection = connection  # BEFORE all dispatch / blocked response reads
    response = None
    try:
        if handle.is_cancelled():
            raise _OllamaRequestCancelled()
        connection.connect()
        # Also supports a mocked connection without the native subclass hook.
        handle.attach_socket(connection.sock)
        connection.sock.settimeout(read_timeout)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        if handle.is_cancelled():
            raise _OllamaRequestCancelled()
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        result = _OllamaResponse(response.status, response.read(), response.getheaders())
        if handle.is_cancelled():
            result.close()
            raise _OllamaRequestCancelled()
        return result
    except TimeoutError as exc:
        if handle.is_cancelled():
            raise _OllamaRequestCancelled() from exc
        raise requests.Timeout(str(exc)) from exc
    except (OSError, http.client.HTTPException) as exc:
        if handle.is_cancelled():
            raise _OllamaRequestCancelled() from exc
        raise requests.ConnectionError(str(exc)) from exc
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            connection.close()


def _release_http_handle(session_id: str, response) -> None:
    with _active_http_lock:
        bucket = _active_http_responses.get(session_id)
        if not bucket:
            return
        for item in tuple(bucket):
            if item is response or getattr(item, "response", None) is response:
                bucket.discard(item)
        if not bucket:
            _active_http_responses.pop(session_id, None)


def _ollama_request(session: ChatSession, method: str, path: str, run_id: str = "", **kwargs):
    """Wait cancelably for a bounded worker owning the full request/body read."""
    run_id = run_id or session.current_run_id
    with session.run_state_lock:
        if _cancelled(session, run_id):
            raise _OllamaRequestCancelled()
        cancel_event = session.run_events.get(run_id, session.cancel_event)
    while not _ollama_worker_slots.acquire(timeout=0.02):
        if cancel_event.is_set() or _cancelled(session, run_id):
            raise _OllamaRequestCancelled()
    handle = _OllamaRequestHandle(session.id, run_id, cancel_event)
    with session.run_state_lock:
        if _cancelled(session, run_id):
            _ollama_worker_slots.release()
            raise _OllamaRequestCancelled()
        _track_http_response(session.id, handle, True)

    def perform():
        try:
            if handle.is_cancelled():
                raise _OllamaRequestCancelled()
            handle.response = _ollama_http_request(handle, method, f"{_ollama_base_url()}{path}", **kwargs)
            if handle.is_cancelled() and handle.response is not None:
                _close_response_async(handle.response)
        except Exception as exc:
            handle.error = exc
        finally:
            _track_http_response(session.id, handle, False)
            _ollama_worker_slots.release()
            handle.done.set()

    try:
        threading.Thread(target=perform, daemon=True, name="ollama-request").start()
    except Exception:
        _track_http_response(session.id, handle, False)
        _ollama_worker_slots.release()
        raise
    while not handle.done.wait(0.02):
        if handle.is_cancelled() or _cancelled(session, run_id):
            handle.abort()
            raise _OllamaRequestCancelled()
    if handle.is_cancelled() or _cancelled(session, run_id):
        handle.abort()
        raise _OllamaRequestCancelled()
    if handle.error is not None:
        raise handle.error
    return handle.response


def _ollama_capabilities(model: str) -> set[str]:
    """Read capabilities from Ollama's authoritative /api/show response."""
    import requests
    try:
        r = requests.post(f"{_ollama_base_url()}/api/show", json={"name": model}, timeout=5)
        if r.status_code != 200:
            return set()
        data = r.json() or {}
        caps = data.get("capabilities", [])
        return {str(c).lower() for c in caps if c}
    except Exception:
        return set()


def _ollama_history(session: ChatSession, system: str, current_user: str, *, include_images=False,
                    image_b64: str = "") -> list[dict]:
    """Bound resumed history and emit only complete, same-turn native pairs."""
    raw_units = []
    raw = []
    for msg in session.messages[:-1]:
        if msg.role == "user" and raw:
            raw_units.append(raw)
            raw = []
        raw.append(msg)
    if raw:
        raw_units.append(raw)
    units = []
    for records in raw_units:
        unit = []
        emitted = set()
        for index, msg in enumerate(records):
            if msg.role in ("user", "assistant"):
                unit.append({"role": msg.role, "content": (msg.content or "")[:12000]})
            elif msg.role == "tool_call" and msg.tool_id and msg.tool_id not in emitted:
                result = next((item for item in records[index + 1:]
                               if item.role == "tool_result" and item.tool_id == msg.tool_id
                               and item.tool_name in ("", msg.tool_name)), None)
                if result is None:
                    continue
                emitted.add(msg.tool_id)
                unit.append({"role": "assistant", "content": "", "tool_calls": [{
                    "id": msg.tool_id, "type": "function", "function": {
                        "name": msg.tool_name, "arguments": dict(msg.tool_args or {})}}]})
                unit.append({"role": "tool", "tool_name": msg.tool_name,
                             "tool_call_id": msg.tool_id, "content": (result.content or "")[:1500]})
        if unit:
            units.append(unit)
    budget = min(MAX_CONTEXT_CHARS, max(1024, int(session.context_length or OLLAMA_DEFAULT_CONTEXT)) * 4)
    current_user = current_user[:max(1024, budget - len(system))]
    selected = []
    used = len(system) + len(current_user)
    for unit in reversed(units):
        size = sum(len(json.dumps(item, ensure_ascii=False)) for item in unit)
        if used + size > budget:
            break
        selected[0:0] = unit
        used += size
    user = {"role": "user", "content": current_user}
    if include_images and image_b64:
        user["images"] = [image_b64]
    return [{"role": "system", "content": system}] + selected + [user]


def _native_tool_calls(message: dict) -> list[dict]:
    calls = message.get("tool_calls") or []
    normalized = []
    for index, call in enumerate(calls):
        fn = call.get("function", {}) if isinstance(call, dict) else {}
        name = fn.get("name") or call.get("name", "")
        args = fn.get("arguments", call.get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except (ValueError, TypeError):
                args = {}
        if not isinstance(args, dict):
            args = {}
        normalized.append({"id": call.get("id") or f"ollama-{uuid.uuid4().hex}", "name": name, "args": args or {}})
    return normalized


def _bound_ollama_messages(messages: list[dict], context_length: int, output_tokens: int,
                           tools: list | None, current_user_index: int) -> list[dict]:
    """Trim whole exchanges, preserving the task and never orphaning native results."""
    limit = min(131072, max(1024, int(context_length)) * 4)
    budget = max(2048, limit - len(json.dumps(tools or [])) - int(output_tokens) * 4)
    image_indices = [i for i, msg in enumerate(messages) if msg.get("images")]
    latest_image = image_indices[-1] if image_indices else -1
    clean = []
    for i, msg in enumerate(messages):
        item = dict(msg)
        if i != latest_image:
            item.pop("images", None)
        if isinstance(item.get("content"), str):
            item["content"] = item["content"][:min(12000, budget)]
        clean.append(item)
    core = {0, current_user_index}
    units = []
    i = 1
    while i < len(clean):
        if i == current_user_index:
            i += 1
            continue
        msg = clean[i]
        if msg.get("tool_calls"):
            indices = [i]
            j = i + 1
            while j < len(clean) and clean[j].get("role") == "tool":
                indices.append(j)
                j += 1
            ids = {call.get("id") for call in msg["tool_calls"]}
            results = {clean[index].get("tool_call_id") for index in indices[1:]}
            if ids and ids.issubset(results):
                units.append(indices)
            i = j
        elif msg.get("role") == "tool":
            i += 1  # malformed orphan; never send it to the native API
        else:
            units.append([i])
            i += 1
    def cost(indices):
        return sum(len(json.dumps({k:v for k,v in clean[index].items() if k != "images"}, ensure_ascii=False)) for index in indices)
    selected = set(core)
    newest = units[-1] if units else []
    reserve = min(cost(newest), max(768, budget // 2))
    used = cost(core)
    if used + reserve > budget:
        system_limit = max(400, min(1400, (budget - reserve) // 2))
        content = clean[0].get("content", "")
        if len(content) > system_limit:
            clean[0]["content"] = content[:200] + "\n...\n" + content[-(system_limit - 205):]
        user_limit = max(128, budget - cost([0]) - reserve - 100)
        content = clean[current_user_index].get("content", "")
        import re
        task_marker = re.search(r"(?:^|\n)Device: [^\n]+\n\n", content)
        if task_marker:
            content = "[Screen context shortened; use the paired tool result]\n" + content[task_marker.end():]
        if len(content) > user_limit:
            marker = "\n[Earlier context shortened]\n"
            keep = max(1, (user_limit - len(marker)) // 2)
            content = content[:keep] + marker + content[-keep:]
        clean[current_user_index]["content"] = content
        used = cost(core)
    # Real accessibility trees are larger than inert test fixtures. Retain the
    # complete newest native call/result pair, but explicitly shorten its text
    # rather than aborting ordinary multi-step tasks at the next model turn.
    for _ in range(8):
        if used + cost(newest) <= budget:
            break
        candidates = [i for i in newest if clean[i].get("role") == "tool" and len(clean[i].get("content", "")) > 256]
        if not candidates:
            break
        index = max(candidates, key=lambda i: len(clean[i]["content"]))
        content = clean[index]["content"]
        length = max(256, len(content) // 2)
        marker = "\n[Tool output shortened for context]\n"
        keep = max(1, (length - len(marker)) // 2)
        clean[index]["content"] = content[:keep] + marker + content[-keep:]
    if used + cost(newest) > budget:
        raise ValueError("Context too small for the latest tool exchange; increase Context in Agent Options (16384 recommended)")
    selected.update(newest)
    used += cost(newest)
    for unit in reversed(units):
        if unit == newest:
            continue
        size = cost(unit)
        if used + size <= budget:
            selected.update(unit)
            used += size
    return [clean[index] for index in sorted(selected)]


def _chat_ollama(session: ChatSession, user_message: str, run_id: str = ""):
    """Use Ollama native tools when advertised, with a legacy parser fallback."""
    import requests

    run_id = run_id or session.current_run_id
    if _cancelled(session, run_id):
        yield {"type": "stopped", "content": "Agent stopped before starting"}
        yield {"type": "done"}
        return
    _append_run_message(session, run_id, ChatMessage(role="user", content=user_message))
    model = session.model or OLLAMA_DEFAULT_MODEL
    system = DEFAULT_SYSTEM.replace("{tool_list}", tool_prompt_list(tools_for_device(session.device)))
    system = system_prompt_for_device(session.device, system)
    capabilities = _ollama_capabilities(model)
    native_tools = "tools" in capabilities
    vision = "vision" in capabilities
    if native_tools:
        system = system_prompt_for_device(session.device, ANTHROPIC_SYSTEM)
        system += f"\nThe target device is fixed to {session.device}. Use native function calls, not JSON code blocks."
    if _cancelled(session, run_id):
        yield {"type": "stopped", "content": "Agent stopped before screen access"}
        yield {"type": "done"}
        return
    current_image = ""
    context = ""
    try:
        tree = get_screen_tree(session.device)
        state = get_phone_state(session.device)
        context = f"[Screen]\n{tree[:1500]}\n[App: {state.get('currentApp', '?')}]\n\n"
    except Exception:
        pass
    if session.auto_screenshot and vision:
        try:
            current_image = get_screenshot_b64(session.device) or ""
        except Exception:
            current_image = ""
    current_user = f"{context}Device: {session.device}\n\n{user_message}"
    messages = _ollama_history(session, system, current_user, include_images=vision, image_b64=current_image)
    if not native_tools:
        # Older JSON-tool models expect textual user results, not native tool records.
        for item in messages:
            if item.get("tool_calls"):
                item["content"] = "Previous tool calls: " + json.dumps(item.pop("tool_calls"))
            if item.get("role") == "tool":
                item["role"] = "user"
                item["content"] = "Tool result: " + item.get("content", "")
                item.pop("tool_call_id", None)
                item.pop("tool_name", None)
    current_user_index = len(messages) - 1
    oai_tools = openai_tools_for_device(session.device) if native_tools else None
    turns = max(1, min(int(session.max_turns or OLLAMA_DEFAULT_TURNS), 24))
    output_tokens = max(64, min(int(session.max_output_tokens or OLLAMA_DEFAULT_OUTPUT), 16384))
    is_thinking_model = any(t in model.lower() for t in ("gemma-4", "gemma4", "ghost-gemma", "qwen3", "deepseek-r1"))

    for _turn in range(turns):
        if _cancelled(session, run_id):
            yield {"type": "stopped", "content": "Agent stopped"}
            yield {"type": "done"}
            return
        bounded = _bound_ollama_messages(messages, session.context_length, output_tokens,
                                         oai_tools, current_user_index)
        payload = {"model": model, "messages": bounded, "stream": False,
                   "options": {"num_ctx": int(session.context_length or OLLAMA_DEFAULT_CONTEXT),
                               "num_predict": output_tokens}}
        if native_tools:
            payload["tools"] = oai_tools
        if is_thinking_model:
            payload["think"] = False
        response = None
        try:
            response = _ollama_request(session, "POST", "/api/chat", run_id=run_id, json=payload, timeout=120)
            data = response.json()
            if response.status_code != 200:
                error = data.get("error", response.text[:200])
                yield {"type": "error", "content": f"Ollama error: {error}"}
                return
        except _OllamaRequestCancelled:
            yield {"type": "stopped", "content": "Agent stopped while Ollama was responding"}
            yield {"type": "done"}
            return
        except requests.ConnectionError:
            yield {"type": "error", "content": f"Ollama not reachable at {_ollama_base_url()}"}
            return
        except Exception as exc:
            if _cancelled(session, run_id):
                yield {"type": "stopped", "content": "Agent stopped"}
            else:
                yield {"type": "error", "content": "Ollama request failed"}
            return
        finally:
            if response is not None:
                _release_http_handle(session.id, response)
                _close_response_async(response)

        if _cancelled(session, run_id):
            yield {"type": "stopped", "content": "Agent stopped before processing the model reply"}
            yield {"type": "done"}
            return
        msg = data.get("message", {}) or {}
        thinking = msg.get("thinking", "") or ""
        if thinking:
            yield {"type": "thinking", "content": thinking}
        reply = msg.get("content", "") or ""
        native_calls = _native_tool_calls(msg) if native_tools else []
        if reply:
            _append_run_message(session, run_id, ChatMessage(role="assistant", content=reply))
            yield {"type": "text", "content": reply}
        if not native_calls and not native_tools:
            for call in _parse_tool_calls(reply):
                name, args = normalize_tool_call(call)
                native_calls.append({"id": f"legacy-{_turn}-{len(native_calls)}", "name": name, "args": args})
        if not native_calls:
            break

        if native_tools:
            messages.append({"role": "assistant", "content": reply, "tool_calls": [
                {"id": call["id"], "function": {"name": call["name"], "arguments": call["args"]}}
                for call in native_calls]})
        else:
            messages.append({"role": "assistant", "content": reply})
        for call in native_calls:
            if _cancelled(session, run_id):
                yield {"type": "stopped", "content": "Agent stopped before device action"}
                yield {"type": "done"}
                return
            name, args = call["name"], dict(call.get("args") or {})
            args.setdefault("device", session.device)
            tool_id = call["id"]
            _append_run_message(session, run_id, ChatMessage(role="tool_call", tool_name=name, tool_args=args, tool_id=tool_id, content=""))
            yield {"type": "tool_call", "name": name, "args": args}
            try:
                result = _dispatch_tool(session, name, args, run_id)
            except Exception as exc:
                result = f"Tool error: {exc}"
            if _cancelled(session, run_id):
                yield {"type": "stopped", "content": "Agent stopped after device action"}
                yield {"type": "done"}
                return
            screenshot = ""
            if name in ("screenshot", "screenshot_annotated", "screenshot_cropped") and vision:
                try:
                    screenshot = get_screenshot_b64(session.device) or ""
                except Exception:
                    screenshot = ""
            if _cancelled(session, run_id):
                yield {"type": "stopped", "content": "Agent stopped"}
                yield {"type": "done"}
                return
            _append_run_message(session, run_id, ChatMessage(role="tool_result", content=result[:1500], tool_name=name,
                                                 tool_id=tool_id, image_b64=screenshot))
            yield {"type": "tool_result", "name": name, "result": result[:500]}
            if screenshot:
                yield {"type": "screenshot", "image": screenshot}
            tool_message = {"role": "tool", "tool_name": name, "tool_call_id": tool_id, "content": result[:1500]}
            if screenshot:
                tool_message["images"] = [screenshot]
            if not native_tools:
                tool_message = {"role": "user", "content": f"Tool {name} result: {result[:1500]}"}
                if screenshot:
                    tool_message["images"] = [screenshot]
            messages.append(tool_message)

    if _cancelled(session, run_id):
        yield {"type": "stopped", "content": "Agent stopped"}
    yield {"type": "done"}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _build_vision_content(session: ChatSession, text: str) -> list:
    """Build user content with screenshot for vision-capable providers."""
    content = []
    if session.auto_screenshot and session.device:
        try:
            tree = get_screen_tree(session.device)
            if tree and tree != "(empty screen)":
                content.append({"type": "text", "text": f"[Current screen]\n{tree[:2000]}"})
        except Exception:
            pass
        try:
            state = get_phone_state(session.device)
            if state:
                content.append(
                    {"type": "text", "text": f"[App: {state.get('currentApp', '')} ({state.get('packageName', '')})]"}
                )
        except Exception:
            pass
        try:
            img = get_screenshot_b64(session.device)
            if img:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}})
        except Exception:
            pass
    content.append({"type": "text", "text": text})
    return content
