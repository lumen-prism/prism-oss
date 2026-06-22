"""
Prism OSS — minimal FastAPI backend.

Features: chat (send + live SSE + message list), code (CLI terminal session
list/detail, live xterm via SSE), and usage (Claude + Codex usage panels).
"""

import asyncio, html, json, sqlite3, os, hashlib, time, secrets, re, logging, subprocess, sys, uuid, threading
import base64 as _b64
import urllib.request as _urllib_request
from typing import Optional, List
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path as _Path
from fastapi import FastAPI, Query, HTTPException, Header, Depends, Request, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse as _StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Load .env if present (no dep)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

_PW = os.environ.get("DASHBOARD_PASSWORD")
if not _PW:
    raise RuntimeError("DASHBOARD_PASSWORD env var not set. Create ~/dashboard/.env with DASHBOARD_PASSWORD=...")
PASSWORD_HASH = hashlib.sha256(_PW.encode()).hexdigest()
del _PW
_TOKEN_STORE = os.path.expanduser("~/.cache/prism-oss/tokens.json")
_HOME = os.path.expanduser("~")

def _load_tokens():
    try:
        with open(_TOKEN_STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(token) for token in data if isinstance(token, str) and token}
    except FileNotFoundError:
        pass
    except Exception as exc:
        logging.warning("failed to load dashboard tokens: %s", exc)
    return set()

def _save_tokens():
    try:
        os.makedirs(os.path.dirname(_TOKEN_STORE), mode=0o700, exist_ok=True)
        tmp = _TOKEN_STORE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sorted(valid_tokens), f)
        os.replace(tmp, _TOKEN_STORE)
    except Exception as exc:
        logging.warning("failed to save dashboard tokens: %s", exc)

valid_tokens = _load_tokens()
BJT = timedelta(hours=8)

# Login rate limiting: IP -> (fail_count, last_fail_time)
login_attempts = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300  # 5 minutes

app = FastAPI(title="Prism Dashboard", docs_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def no_cache_frontend_assets(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if (
        path in {"/app", "/dashboard/app"}
        or (
            (path.startswith("/static/") or path.startswith("/dashboard/static/"))
            and path.rsplit(".", 1)[-1] in {"js", "css"}
        )
    ):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response

# --- Auth ---
class AuthRequest(BaseModel):
    password: str


@app.post("/api/auth")
def authenticate(req: AuthRequest, x_real_ip: Optional[str] = Header(None), x_forwarded_for: Optional[str] = Header(None)):
    ip = x_real_ip or (x_forwarded_for.split(",")[0].strip() if x_forwarded_for else "unknown")
    # Check rate limit
    if ip in login_attempts:
        fails, last_time = login_attempts[ip]
        if fails >= MAX_LOGIN_ATTEMPTS and time.time() - last_time < LOGIN_LOCKOUT_SECONDS:
            remaining = int(LOGIN_LOCKOUT_SECONDS - (time.time() - last_time))
            raise HTTPException(429, f"尝试太多次了，请{remaining}秒后再试")
        if time.time() - last_time >= LOGIN_LOCKOUT_SECONDS:
            login_attempts.pop(ip, None)
    if hashlib.sha256(req.password.encode()).hexdigest() == PASSWORD_HASH:
        login_attempts.pop(ip, None)
        token = secrets.token_hex(32)
        valid_tokens.add(token)
        _save_tokens()
        return {"success": True, "token": token}
    # Record failed attempt
    fails, _ = login_attempts.get(ip, (0, 0))
    login_attempts[ip] = (fails + 1, time.time())
    raise HTTPException(401, "密码不对哦")

def require_auth(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    if authorization[7:] not in valid_tokens:
        raise HTTPException(401, "登录已过期")
    return True

# --- Static / pages ---
static_path = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_path), name="static")
app.mount("/dashboard/static", StaticFiles(directory=static_path), name="static_prefixed")
CHAT_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_data")
os.makedirs(CHAT_DATA_DIR, exist_ok=True)

@app.get("/")
async def serve_root():
    return FileResponse(os.path.join(static_path, "app.html"), headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"})

@app.get("/app")
async def serve_app():
    return FileResponse(os.path.join(static_path, "app.html"), headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"})

# --- Helper modules ---
import sys as _sys
_cc_dir = os.path.dirname(os.path.abspath(__file__))
if _cc_dir not in _sys.path:
    _sys.path.insert(0, _cc_dir)
import terminal_manager as _tm
import message_store as _ms

# --- SSE / chat-event infra ---

class _NewSessionReq(BaseModel):
    name: str
    cwd: str
    session_type: str = "cc"  # "cc", "shell", "codex", or "opencode"
    cols: int = 80
    rows: int = 24


def _require_auth_qs(token: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    # SSE: EventSource can't set headers, so accept ?token=
    if authorization and authorization.startswith("Bearer ") and authorization[7:] in valid_tokens:
        return True
    if token and token in valid_tokens:
        return True
    raise HTTPException(401, "未登录")


def _sse_payload(event: str, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _stat_fingerprint(path) -> Optional[str]:
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{path}:{st.st_size}:{st.st_mtime_ns}"


_CHAT_EVENT_INTERVALS = {
    "cc": 0.12,
    "codex": 0.12,
    "opencode": 0.18,
}
_CHAT_EVENT_FALLBACK_INTERVAL = 0.45
_CHAT_EVENT_SOURCE_REFRESH_SECONDS = 5.0
_OPENCODE_SSE_URL = os.environ.get("OPENCODE_SSE_URL", "http://127.0.0.1:43210/event")
_OPENCODE_NATIVE_RETRY_SECONDS = 3.0
_OPENCODE_NATIVE_FALLBACK_POLL_SECONDS = 1.5
_OPENCODE_NATIVE_EVENT_PREFIXES = (
    "message.",
    "session.next.",
    "session.status",
    "session.idle",
    "session.updated",
)


def _chat_event_interval(payload: dict) -> float:
    kind = payload.get("kind") or ""
    if payload.get("source"):
        return _CHAT_EVENT_INTERVALS.get(kind, _CHAT_EVENT_FALLBACK_INTERVAL)
    return _CHAT_EVENT_FALLBACK_INTERVAL


def _opencode_chat_fingerprint_for_session(session_id: str | None) -> Optional[str]:
    if not session_id:
        return None
    db = _tm.OPENCODE_DB_PATH
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        try:
            msg_updated, msg_count = conn.execute(
                "SELECT COALESCE(MAX(time_updated), 0), COUNT(*) FROM message WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            part_updated, part_count = conn.execute(
                "SELECT COALESCE(MAX(time_updated), 0), COUNT(*) FROM part WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return f"opencode:{session_id}:db-unavailable"
    return f"opencode:{session_id}:{msg_updated}:{msg_count}:{part_updated}:{part_count}"


def _opencode_chat_fingerprint(info: dict) -> Optional[str]:
    session_id = info.get("opencode_session_id")
    if not session_id:
        try:
            session_id = _tm._find_opencode_session(info.get("cwd"), info.get("opencode_pid"))
        except Exception:
            session_id = None
    return _opencode_chat_fingerprint_for_session(session_id)


def _chat_event_target(name: str) -> dict:
    info = _tm._pane_info(name) or {}
    kind = info.get("kind") or "missing"
    if kind == "codex":
        jsonl = _find_codex_jsonl(info.get("codex_pid"), session_name=name)
        if jsonl:
            return {"name": name, "kind": kind, "source": str(jsonl), "source_type": "stat"}
    if kind == "opencode":
        session_id = info.get("opencode_session_id")
        if not session_id:
            try:
                session_id = _tm._find_opencode_session(info.get("cwd"), info.get("opencode_pid"))
            except Exception:
                session_id = None
        if session_id:
            return {"name": name, "kind": kind, "source": session_id, "source_type": "opencode"}
    if kind == "cc":
        jsonl = _tm._claude_jsonl_for_pid(info.get("claude_pid"))
        if jsonl:
            return {"name": name, "kind": kind, "source": str(jsonl), "source_type": "stat"}
    log_p = _tm.log_path(name)
    return {"name": name, "kind": kind, "source": str(log_p), "source_type": "stat"}


def _chat_event_payload(target: dict) -> dict:
    name = target.get("name") or ""
    kind = target.get("kind") or "missing"
    source = target.get("source") or ""
    source_type = target.get("source_type") or ""
    fingerprint = None
    if source_type == "opencode":
        fingerprint = _opencode_chat_fingerprint_for_session(source)
    elif source_type == "stat":
        fingerprint = _stat_fingerprint(source)
    if not fingerprint:
        log_p = _tm.log_path(name)
        fingerprint = _stat_fingerprint(str(log_p)) or f"{kind}:unavailable:{int(time.time() // 15)}"
        source = source or str(log_p)
    return {"name": name, "kind": kind, "source": source, "fingerprint": fingerprint}


def _chat_event_fingerprint(name: str) -> dict:
    return _chat_event_payload(_chat_event_target(name))


async def _opencode_sse_data(response):
    data_lines = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)


def _opencode_event_session_id(event: dict) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
    props = payload.get("properties") if isinstance(payload, dict) else None
    if isinstance(props, dict):
        return props.get("sessionID") or ""
    return ""


def _opencode_event_relevant(event: dict, session_id: str) -> bool:
    if not session_id:
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
    if not isinstance(payload, dict):
        return False
    event_type = payload.get("type") or ""
    if not any(event_type.startswith(prefix) for prefix in _OPENCODE_NATIVE_EVENT_PREFIXES):
        return False
    return _opencode_event_session_id(event) == session_id


def _opencode_native_payload(name: str, session_id: str, event: dict) -> dict:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
    event_id = payload.get("id") or f"{time.time():.6f}"
    event_type = payload.get("type") or "opencode.event"
    return {
        "name": name,
        "kind": "opencode",
        "source": session_id,
        "source_type": "opencode-native",
        "event_type": event_type,
        "fingerprint": f"opencode-native:{session_id}:{event_id}",
        "ts": time.time(),
    }


async def _opencode_native_chat_events(name: str, request: Request, target: dict):
    session_id = target.get("source") or ""
    last_fp = None
    last_ping = time.monotonic()

    def emit_if_changed(payload: dict) -> Optional[str]:
        nonlocal last_fp
        fp = payload.get("fingerprint")
        if not fp or fp == last_fp:
            return None
        last_fp = fp
        payload["ts"] = payload.get("ts") or time.time()
        return _sse_payload("chat_update", payload)

    initial = emit_if_changed(_chat_event_payload(target))
    if initial:
        yield initial

    try:
        import httpx
    except Exception:
        httpx = None

    while not await request.is_disconnected():
        if not httpx:
            retry_until = time.monotonic() + _OPENCODE_NATIVE_RETRY_SECONDS
            while time.monotonic() < retry_until and not await request.is_disconnected():
                fallback = emit_if_changed(_chat_event_payload(target))
                if fallback:
                    yield fallback
                await asyncio.sleep(_chat_event_interval({"kind": "opencode", "source": session_id}))
            continue

        try:
            timeout = httpx.Timeout(connect=2.0, read=None, write=2.0, pool=2.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("GET", _OPENCODE_SSE_URL, headers={"Accept": "text/event-stream"}) as response:
                    response.raise_for_status()
                    stream = _opencode_sse_data(response)
                    while not await request.is_disconnected():
                        now = time.monotonic()
                        if now - last_ping >= 15:
                            last_ping = now
                            yield ": ping\n\n"
                        try:
                            raw = await asyncio.wait_for(anext(stream), timeout=_OPENCODE_NATIVE_FALLBACK_POLL_SECONDS)
                        except asyncio.TimeoutError:
                            fallback = emit_if_changed(_chat_event_payload(target))
                            if fallback:
                                yield fallback
                            continue
                        event = json.loads(raw)
                        if not _opencode_event_relevant(event, session_id):
                            continue
                        native = emit_if_changed(_opencode_native_payload(name, session_id, event))
                        if native:
                            yield native
        except Exception:
            retry_until = time.monotonic() + _OPENCODE_NATIVE_RETRY_SECONDS
            while time.monotonic() < retry_until and not await request.is_disconnected():
                fallback = emit_if_changed(_chat_event_payload(target))
                if fallback:
                    yield fallback
                now = time.monotonic()
                if now - last_ping >= 15:
                    last_ping = now
                    yield ": ping\n\n"
                await asyncio.sleep(_chat_event_interval({"kind": "opencode", "source": session_id}))


@app.get("/api/sessions/{name}/chat-events")
async def api_sessions_chat_events(name: str, request: Request, _=Depends(_require_auth_qs)):
    """Lightweight live-change stream for Chat detail views.

    This does not replace the existing parsers. It only tells the browser when
    the session's backing transcript changed so the normal /chat-messages fetch
    can run immediately instead of waiting for the polling interval.
    """
    if not _tm.NAME_RE.match(name):
        raise HTTPException(400, "invalid session name")

    target = _chat_event_target(name)
    if target.get("kind") == "opencode" and target.get("source_type") == "opencode":
        return _StreamingResponse(
            _opencode_native_chat_events(name, request, target),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    async def events():
        last_fp = None
        last_ping = time.monotonic()
        current_target = target
        next_target_refresh = time.monotonic() + _CHAT_EVENT_SOURCE_REFRESH_SECONDS
        while not await request.is_disconnected():
            now = time.monotonic()
            if now >= next_target_refresh:
                current_target = _chat_event_target(name)
                next_target_refresh = now + _CHAT_EVENT_SOURCE_REFRESH_SECONDS
            payload = _chat_event_payload(current_target)
            fp = payload.get("fingerprint")
            if fp and fp != last_fp:
                last_fp = fp
                payload["ts"] = time.time()
                yield _sse_payload("chat_update", payload)
            if now - last_ping >= 15:
                last_ping = now
                yield ": ping\n\n"
            await asyncio.sleep(_chat_event_interval(payload))

    return _StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

@app.get("/api/sessions")
def api_sessions_list(_=Depends(require_auth)):
    return {"sessions": _tm.list_sessions()}


RECOVERY_STATUS_FILE = os.path.join(CHAT_DATA_DIR, "recovery", "session_recovery_status.json")


def _read_recovery_statuses() -> dict:
    try:
        with open(RECOVERY_STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


@app.get("/api/sessions/{name}/recovery-status")
def api_session_recovery_status(name: str, _=Depends(require_auth)):
    item = _read_recovery_statuses().get(name)
    if not isinstance(item, dict):
        return {"session": name, "state": "none"}
    return item


RECOVER_SCRIPT = os.path.join(os.path.dirname(__file__), "scripts", "recover_cc_session.py")


@app.get("/api/recovery/status")
def api_recovery_status_all(_=Depends(require_auth)):
    """Return current recovery status for all sessions."""
    return _read_recovery_statuses()


@app.post("/api/recovery/approve/{session_name}")
def api_recovery_approve(session_name: str, _=Depends(require_auth)):
    """Approve and execute a pending recovery."""
    import subprocess as _sp
    if not re.match(r"^[A-Za-z0-9_.-]+$", session_name or ""):
        raise HTTPException(400, "invalid session name")
    try:
        proc = _sp.run(
            ["python3", RECOVER_SCRIPT, "--session", session_name, "--apply", "--approve"],
            capture_output=True, text=True, timeout=30,
        )
        try:
            result = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            result = {"stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode}
        if proc.returncode != 0:
            result["ok"] = False
            if not result.get("error"):
                result["error"] = proc.stderr.strip() or f"exit code {proc.returncode}"
        return result
    except _sp.TimeoutExpired:
        raise HTTPException(504, "recovery script timed out")
    except Exception as exc:
        raise HTTPException(500, f"recovery script error: {exc}")


@app.post("/api/recovery/dismiss/{session_name}")
def api_recovery_dismiss(session_name: str, _=Depends(require_auth)):
    """Dismiss a pending recovery without recovering."""
    import subprocess as _sp
    if not re.match(r"^[A-Za-z0-9_.-]+$", session_name or ""):
        raise HTTPException(400, "invalid session name")
    try:
        proc = _sp.run(
            ["python3", RECOVER_SCRIPT, "--session", session_name, "--dismiss"],
            capture_output=True, text=True, timeout=10,
        )
        try:
            result = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            result = {"stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode}
        return result
    except _sp.TimeoutExpired:
        raise HTTPException(504, "dismiss script timed out")
    except Exception as exc:
        raise HTTPException(500, f"dismiss error: {exc}")



@app.get("/api/codex/models")
def api_codex_models(_=Depends(require_auth)):
    import subprocess
    try:
        result = subprocess.run(
            ["codex", "debug", "models"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as e:
        raise HTTPException(500, f"无法读取 Codex 模型列表: {e}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "codex debug models failed").strip()
        raise HTTPException(500, detail[:300])
    try:
        raw = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        raise HTTPException(500, "Codex 模型列表不是有效 JSON")
    models = []
    for item in raw.get("models") or []:
        if item.get("visibility") != "list":
            continue
        efforts = [
            level.get("effort")
            for level in (item.get("supported_reasoning_levels") or [])
            if level.get("effort")
        ]
        if not item.get("slug"):
            continue
        models.append({
            "slug": item.get("slug"),
            "name": item.get("display_name") or item.get("slug"),
            "desc": item.get("description") or "",
            "efforts": efforts,
            "default_effort": item.get("default_reasoning_level") or "",
        })
    return {"models": models}


@app.get("/api/dirs")
def api_list_dirs(path: str = _HOME, _=Depends(require_auth)):
    real = os.path.realpath(os.path.expanduser(path))
    if not (real == _HOME or real.startswith(_HOME + "/")):
        raise HTTPException(400, f"path must be under {_HOME}")
    if not os.path.isdir(real):
        raise HTTPException(400, "not a directory")
    try:
        entries = sorted(
            e for e in os.listdir(real)
            if not e.startswith('.') and os.path.isdir(os.path.join(real, e))
        )
    except PermissionError:
        raise HTTPException(403, "permission denied")
    return {"path": real, "dirs": entries}

@app.post("/api/sessions/new")
def api_sessions_new(req: _NewSessionReq, _=Depends(require_auth)):
    res = _tm.create_session(
        req.name, req.cwd,
        session_type=req.session_type,
        cols=req.cols,
        rows=req.rows,
    )
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "create failed"))
    return res

@app.delete("/api/sessions/{name}")
def api_sessions_kill(name: str, _=Depends(require_auth)):
    res = _tm.kill_session(name)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "kill failed"))
    # Keep uploaded attachments so cross-session image/file search remains usable
    # after a live chat is closed.
    return res


class _RenameSessionReq(BaseModel):
    name: str


@app.post("/api/sessions/{name}/rename")
def api_sessions_rename(name: str, req: _RenameSessionReq, _=Depends(require_auth)):
    new_name = (req.name or "").strip()
    res = _tm.rename_session(name, new_name)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "rename failed"))
    if name != new_name:
        try:
            old_dir = os.path.join(_UPLOAD_ROOT, name)
            new_dir = os.path.join(_UPLOAD_ROOT, new_name)
            if os.path.isdir(old_dir) and not os.path.exists(new_dir):
                os.rename(old_dir, new_dir)
        except Exception:
            pass
    return res

class _TitleReq(BaseModel):
    title: str


@app.post("/api/sessions/{name}/title")
def api_sessions_title(name: str, req: _TitleReq, _=Depends(require_auth)):
    res = _tm.set_display_name(name, req.title)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "title update failed"))
    return res


class _ChatNameReq(BaseModel):
    chat_name: str


@app.post("/api/sessions/{name}/chat-name")
def api_sessions_chat_name(name: str, req: _ChatNameReq, _=Depends(require_auth)):
    res = _tm.set_chat_name(name, req.chat_name)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "chat-name update failed"))
    return res


@app.post("/api/sessions/{name}/avatar")
async def api_sessions_avatar_upload(name: str, file: UploadFile = File(...), _=Depends(require_auth)):
    import os as _os
    raw = await file.read()
    ext = _os.path.splitext(file.filename or "")[1].lower() or ".jpg"
    res = _tm.save_avatar(name, raw, ext)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "avatar upload failed"))
    return res


@app.delete("/api/sessions/{name}/avatar")
def api_sessions_avatar_clear(name: str, _=Depends(require_auth)):
    return _tm.clear_avatar(name)


@app.get("/api/sessions/{name}/avatar")
def api_sessions_avatar_get(name: str, _=Depends(_require_auth_qs)):
    p = _tm.find_avatar_path(name)
    if p is None:
        raise HTTPException(404, "no avatar")
    media = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
    }.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(str(p), media_type=media)


@app.post("/api/sessions/{name}/archive")
def api_sessions_archive(name: str, _=Depends(require_auth)):
    res = _tm.archive_session(name)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "archive failed"))
    # Archive keeps attachments available for message search and previews.
    return res

@app.get("/api/sessions/{name}/history")
def api_sessions_history(name: str, lines: int = 2000, _=Depends(require_auth)):
    return {"name": name, **_tm.capture_history_snapshot(name, lines)}

def _window_chat_messages(messages, limit=200, focus_uuid=None):
    """Trim a chat message list to a recent window, or center it on a focus hit."""
    visible = list(messages)
    if not limit or len(visible) <= limit:
        return visible
    if focus_uuid:
        target = next((i for i, item in enumerate(visible) if item.get("source_uuid") == focus_uuid), None)
        if target is not None:
            start = max(0, target - max(1, limit // 2))
            return visible[start:start + limit]
    return visible[-limit:]

@app.get("/api/sessions/{name}/chat-messages")
def api_sessions_chat_messages(name: str, limit: int = 200, focus_uuid: Optional[str] = None, focus_id: Optional[int] = None, _=Depends(require_auth)):
    source_limit = 0 if (focus_uuid or focus_id) else max(int(limit or 200) * 2, 240)
    raw_msgs = _tm.list_chat_messages(name, source_limit, focus_uuid=focus_uuid)
    if raw_msgs:
        msgs = _window_chat_messages(raw_msgs, limit, focus_uuid=focus_uuid)
    else:
        raw_msgs = _unified_fallback(name, source_limit, focus_id=focus_id, focus_uuid=focus_uuid)
        msgs = _window_chat_messages(raw_msgs, limit, focus_uuid=focus_uuid)
    compactions = _tm.compaction_messages(name) if msgs else []
    compaction_overview = []
    for message in compactions:
        summary_block = next((block for block in message.get("blocks", []) if block.get("type") == "compaction"), None)
        if not summary_block:
            continue
        compaction_overview.append({
            "source_uuid": message.get("source_uuid"), "ts": message.get("ts"), "role": "system",
            "blocks": [{"type": "compaction", "status": "done", "metadata": summary_block.get("metadata") or {}}],
        })
    terminal_prompt = _tm.detect_terminal_prompt(name)
    return {"name": name, "messages": msgs, "compaction_overview": compaction_overview,
            "terminal_prompt": terminal_prompt}

@app.post("/api/sessions/{name}/terminal-respond")
def api_sessions_terminal_respond(name: str, req: dict, _=Depends(require_auth)):
    """Send keystrokes to a session's terminal to respond to a blocking prompt."""
    keys = req.get("keys") or []
    if not isinstance(keys, list) or not keys:
        return {"ok": False, "error": "missing keys list"}
    return _tm.send_terminal_keys(name, keys)


@app.get("/api/sessions/{name}/compactions")
def api_sessions_compactions(name: str, _=Depends(require_auth)):
    return {"name": name, "compactions": _tm.compaction_messages(name)}

@app.get("/api/sessions/{name}/session-boundaries")
def api_sessions_session_boundaries(name: str, _=Depends(require_auth)):
    return {"name": name, "boundaries": []}

@app.get("/api/sessions/{name}/usage")
def api_sessions_usage(name: str, _=Depends(require_auth)):
    """Context-window usage from the latest assistant turn's reported tokens."""
    info = _tm._pane_info(name) or {}
    kind = info.get("kind")
    if kind == "cc" and info.get("claude_pid"):
        jsonl = _tm._claude_jsonl_for_pid(info["claude_pid"])
        usage_reader = _tm._read_last_usage
    elif kind == "codex" and info.get("codex_pid"):
        jsonl = _find_codex_jsonl(info["codex_pid"], session_name=name)
        usage_reader = _tm._read_last_codex_usage
    elif kind == "opencode":
        usage = _tm._read_opencode_usage(pane_cwd=info.get("cwd"),
                                         session_id=info.get("opencode_session_id"),
                                         opencode_pid=info.get("opencode_pid"))
        if not usage:
            return {"name": name, "kind": kind, "available": False, "reason": "no usage data yet"}
        return {"name": name, "kind": kind, "available": True, **usage}
    else:
        return {"name": name, "kind": kind, "available": False, "reason": "not a cc/codex session"}
    if jsonl is None:
        return {"name": name, "kind": kind, "available": False, "reason": "no active jsonl"}
    usage = usage_reader(jsonl)
    if not usage:
        return {"name": name, "kind": kind, "available": False, "reason": "no usage data yet"}
    return {"name": name, "kind": kind, "available": True, **usage}

@app.get("/api/sessions/{name}/search-id")
def api_sessions_search_id(name: str, _=Depends(require_auth)):
    """Map a visible Chat session to its unified-search session id."""
    info = _tm._pane_info(name) or {}
    if info.get("kind") == "cc" and info.get("claude_pid"):
        jsonl = _tm._claude_jsonl_for_pid(info["claude_pid"])
        if jsonl:
            _ms.import_cc_sessions()
            return {"session_id": jsonl.stem, "source": "cc"}
    if info.get("kind") == "codex" and info.get("codex_pid"):
        jsonl = _find_codex_jsonl(info["codex_pid"], session_name=name)
        if jsonl:
            import re as _re
            match = _re.search(r'rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$', jsonl.name)
            if match:
                _ms.import_codex_sessions()
                return {"session_id": "codex-" + match.group(1), "source": "codex"}
    raise HTTPException(404, "search index unavailable for session")


def _unified_fallback(session_name: str, limit: int, focus_id: Optional[int] = None, focus_uuid: Optional[str] = None):
    """When terminal_manager can't parse messages (e.g. Codex), try unified store."""
    info = _tm._pane_info(session_name)
    if not info:
        return []
    codex_pid = info.get("codex_pid")
    claude_pid = info.get("claude_pid")
    kind = info.get("kind", "shell")

    if kind == "codex" and codex_pid:
        jsonl = _find_codex_jsonl(codex_pid, session_name=session_name)
        if jsonl:
            msgs = _ms.read_codex_chat_messages(jsonl, limit=0)
            return _window_chat_messages(msgs, limit, focus_uuid=focus_uuid)
    elif kind == "cc" and claude_pid:
        jsonl = _tm._claude_jsonl_for_pid(claude_pid)
        store_id = jsonl.stem if jsonl else None
        # Fallback: when JSONL was cleaned up by Claude Code, read sessionId
        # from the per-pid metadata file (~/.claude/sessions/<pid>.json).
        if not store_id:
            meta = _Path.home() / ".claude" / "sessions" / f"{claude_pid}.json"
            if meta.exists():
                try:
                    store_id = json.loads(meta.read_text()).get("sessionId")
                except Exception:
                    pass
        if store_id:
            _ms.import_cc_sessions()
            msgs = _ms.get_session_as_blocks(store_id, limit=limit, focus_id=focus_id)
            return _window_chat_messages(msgs, limit)

    return []


def _find_codex_jsonl(codex_pid: int, session_name: str | None = None):
    """Find the active Codex JSONL for a running codex process."""
    return _tm._find_codex_jsonl(codex_pid, session_name=session_name)


def _find_recent_codex_jsonl_for_pid(codex_pid: int):
    """Fallback for Codex versions that do not keep the rollout JSONL fd open."""
    from pathlib import Path

    sessions_dir = Path(os.path.expanduser("~/.codex/sessions"))
    if not sessions_dir.exists():
        return None
    try:
        proc_stat = os.stat(f"/proc/{codex_pid}")
        proc_started_at = proc_stat.st_ctime
    except OSError:
        return None
    try:
        proc_cwd = os.readlink(f"/proc/{codex_pid}/cwd")
    except OSError:
        proc_cwd = None

    try:
        candidates = sorted(
            sessions_dir.rglob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:50]
    except OSError:
        return None

    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < proc_started_at - 300:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline()
            record = json.loads(first) if first else {}
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload") or {}
        if proc_cwd and payload.get("cwd") and os.path.realpath(payload["cwd"]) != os.path.realpath(proc_cwd):
            continue
        return path
    return None

@app.get("/api/archived-sessions")
def api_archived_sessions_list(_=Depends(require_auth)):
    return {"sessions": _tm.list_archived_sessions()}


@app.get("/api/archived-sessions/search")
def api_archived_sessions_search(q: str = Query(..., min_length=1), _=Depends(require_auth)):
    return {"q": q, "hits": _tm.search_archived(q)}


@app.get("/api/archived-sessions/{archive_id}/messages")
def api_archived_sessions_messages(archive_id: str, limit: int = 200, focus_uuid: Optional[str] = None, _=Depends(require_auth)):
    return {"archive_id": archive_id, "messages": _tm.read_archived_messages(archive_id, limit, focus_uuid=focus_uuid)}


class _RenameArchivedReq(BaseModel):
    display_name: Optional[str] = None


@app.post("/api/archived-sessions/{archive_id}/rename")
def api_archived_sessions_rename(archive_id: str, req: _RenameArchivedReq, _=Depends(require_auth)):
    res = _tm.rename_archived(archive_id, req.display_name)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error", "rename failed"))
    return res


@app.delete("/api/archived-sessions/{archive_id}")
def api_archived_sessions_delete(archive_id: str, _=Depends(require_auth)):
    res = _tm.delete_archived(archive_id)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error", "delete failed"))
    return res

@app.get("/api/sessions/{name}/uploads/{fname}")
def api_sessions_upload_get(name: str, fname: str, _=Depends(_require_auth_qs)):
    if not _tm.NAME_RE.match(name):
        raise HTTPException(400, "invalid session name")
    if "/" in fname or ".." in fname:
        raise HTTPException(400, "invalid filename")
    path = os.path.join(_UPLOAD_ROOT, name, fname)
    if not os.path.exists(path):
        raise HTTPException(404, "not found")
    return FileResponse(path)


_SAFE_DOWNLOAD_ROOTS = [
    os.path.expanduser("~"),
    "/tmp/dashboard-uploads",
    "/tmp",
]

@app.get("/api/files/download")
def api_files_download(path: str = Query(...), _=Depends(_require_auth_qs)):
    real = os.path.realpath(path)
    if not any(real.startswith(os.path.realpath(r) + "/") or real == os.path.realpath(r) for r in _SAFE_DOWNLOAD_ROOTS):
        raise HTTPException(403, "path not allowed")
    if not os.path.isfile(real):
        raise HTTPException(404, "file not found")
    return FileResponse(real, filename=os.path.basename(real))

@app.get("/api/files/check")
def api_files_check(path: str = Query(...), _=Depends(require_auth)):
    real = os.path.realpath(path)
    allowed = any(real.startswith(os.path.realpath(r) + "/") or real == os.path.realpath(r) for r in _SAFE_DOWNLOAD_ROOTS)
    exists = allowed and os.path.isfile(real)
    size = os.path.getsize(real) if exists else 0
    return {"exists": exists, "path": path, "size": size}

@app.get("/api/sessions/{name}/login-state")
def api_sessions_login_state(name: str, _=Depends(require_auth)):
    return _tm.detect_login_state(name)


class _InputReq(BaseModel):
    data: str


class _ChatInputReq(BaseModel):
    data: str


@app.post("/api/sessions/{name}/resize")
def api_sessions_resize(name: str, cols: int = Query(..., ge=10, le=500), rows: int = Query(..., ge=5, le=200), _=Depends(require_auth)):
    res = _tm.resize_session(name, cols, rows)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "resize failed"))
    return res


@app.post("/api/sessions/{name}/input")
def api_sessions_input(name: str, req: _InputReq, _=Depends(require_auth)):
    res = _tm.send_input(name, req.data)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "input failed"))
    return res

@app.post("/api/sessions/{name}/chat-send")
def api_sessions_chat_send(name: str, req: _ChatInputReq, _=Depends(require_auth)):
    if not req.data:
        raise HTTPException(400, "empty input")
    info = _tm._pane_info(name) or {}
    if info.get("kind") not in ("cc", "codex", "opencode"):
        raise HTTPException(400, "session is not an AI chat")
    res = _tm.send_input(name, req.data)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "input failed"))
    return {**res, "queued": False}


_UPLOAD_ROOT = "/tmp/dashboard-uploads"
_UPLOAD_MAX_BYTES = 500 * 1024 * 1024  # 500MB (nginx client_max_body_size matches)

@app.post("/api/sessions/{name}/upload")
async def api_sessions_upload(name: str, file: UploadFile = File(...), _=Depends(require_auth)):
    if not _tm.NAME_RE.match(name):
        raise HTTPException(400, "invalid session name")
    # Sanitize incoming filename: basename only, replace anything that's not
    # alnum/dot/dash/underscore. Prefix with timestamp so repeated uploads of
    # the same name don't collide.
    raw = file.filename or "upload.bin"
    base = os.path.basename(raw)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base) or "upload.bin"
    fname = f"{int(time.time()*1000)}-{safe}"
    session_dir = os.path.join(_UPLOAD_ROOT, name)
    os.makedirs(session_dir, exist_ok=True)
    target = os.path.join(session_dir, fname)
    # Stream to disk in chunks so large uploads (e.g. ~300MB APKs) don't load
    # the whole file into memory. Abort + clean up the moment we cross the cap.
    written = 0
    try:
        with open(target, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)  # 1MB
                if not chunk:
                    break
                written += len(chunk)
                if written > _UPLOAD_MAX_BYTES:
                    raise HTTPException(413, f"file too large (>{_UPLOAD_MAX_BYTES // (1024*1024)}MB)")
                f.write(chunk)
    except HTTPException:
        try:
            os.remove(target)
        except OSError:
            pass
        raise
    return {"ok": True, "path": target, "name": safe, "size": written}


@app.get("/api/sessions/{name}/stream")
async def api_sessions_stream(name: str, request: Request, offset: Optional[int] = Query(None, ge=0), _=Depends(_require_auth_qs)):
    async def is_connected():
        return not await request.is_disconnected()

    return _StreamingResponse(
        _tm.tail_log_sse(name, is_connected, offset),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
            "Connection": "keep-alive",
        },
    )

# --- Unified message store routes ---
_MESSAGE_REFRESH_AT = 0.0
_MESSAGE_REFRESH_TTL = 3.0
_MESSAGE_NAV_AT = 0.0
_MESSAGE_NAV_CACHE = {}


def _refresh_message_index(force: bool = False):
    """Refresh changed source files without rescanning on each keystroke."""
    global _MESSAGE_REFRESH_AT
    now = time.monotonic()
    if force or now - _MESSAGE_REFRESH_AT >= _MESSAGE_REFRESH_TTL:
        result = _ms.import_all(force=force)
        _MESSAGE_REFRESH_AT = now
        return result
    return {"cached": True}


def _message_session_navigation(force: bool = False):
    """Map indexed sessions back to live chats or read-only archives."""
    global _MESSAGE_NAV_AT, _MESSAGE_NAV_CACHE
    now = time.monotonic()
    if not force and now - _MESSAGE_NAV_AT < _MESSAGE_REFRESH_TTL:
        return _MESSAGE_NAV_CACHE
    nav = {}
    try:
        for item in _tm.list_sessions():
            sid = None
            if item.get("kind") == "cc" and item.get("claude_pid"):
                jsonl = _tm._claude_jsonl_for_pid(item["claude_pid"])
                sid = jsonl.stem if jsonl else None
            elif item.get("kind") == "codex" and item.get("codex_pid"):
                jsonl = _find_codex_jsonl(item["codex_pid"], session_name=item.get("name"))
                if jsonl:
                    match = re.search(r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", jsonl.name)
                    sid = "codex-" + match.group(1) if match else None
            if sid:
                live_nav = {
                    "state": "live",
                    "live_name": item.get("name"),
                    "display_name": item.get("chat_name") or item.get("display_name") or item.get("name"),
                    "kind": item.get("kind"),
                }
                nav[sid] = live_nav
        for meta in _tm.list_archived_sessions():
            sid = meta.get("session_id")
            if not sid or sid in nav:
                continue
            nav[sid] = {
                "state": "archived",
                "archive_id": meta.get("archive_id"),
                "name": meta.get("name"),
                "display_name": meta.get("display_name") or meta.get("name"),
                "kind": meta.get("kind"),
                "archived_at": meta.get("archived_at"),
                "jsonl_path": meta.get("jsonl_path"),
            }
    except Exception:
        pass
    _MESSAGE_NAV_CACHE = nav
    _MESSAGE_NAV_AT = now
    return nav


@app.post("/api/messages/import")
def messages_import(_=Depends(require_auth)):
    result = _refresh_message_index(force=True)
    _message_session_navigation(force=True)
    return {"success": True, "imported": result}


@app.get("/api/messages/stats")
def messages_stats(_=Depends(require_auth)):
    return _ms.get_stats()


@app.get("/api/messages/sessions")
def messages_sessions(
    source: Optional[str] = None,
    scope: str = Query("active", pattern="^(active|trash|all)$"),
    limit: int = 50,
    offset: int = 0,
    _=Depends(require_auth),
):
    return {"sessions": _ms.list_sessions(source=source, scope=scope, limit=limit, offset=offset)}


@app.get("/api/messages/session/{session_id}")
def messages_session(session_id: str, limit: int = 500, offset: int = 0, _=Depends(require_auth)):
    msgs = _ms.get_session_messages(session_id, limit=limit, offset=offset)
    if not msgs:
        raise HTTPException(404, "session not found or empty")
    return {"session_id": session_id, "messages": msgs}


@app.get("/api/messages/session/{session_id}/blocks")
def messages_session_blocks(session_id: str, limit: int = 500, focus_id: Optional[int] = None, _=Depends(require_auth)):
    blocks = _ms.get_session_as_blocks(session_id, limit=limit, focus_id=focus_id)
    if not blocks:
        raise HTTPException(404, "session not found or empty")
    return {"session_id": session_id, "messages": blocks}


@app.get("/api/messages/search")
def messages_search(q: str = "", source: Optional[str] = None, type: Optional[str] = None,
                    session_id: Optional[str] = None, overlay_session_id: Optional[str] = None,
                    limit: int = 50, offset: int = 0, _=Depends(require_auth)):
    if not q.strip() and not type:
        raise HTTPException(400, "query or type required")
    _refresh_message_index()
    page_limit = max(1, min(int(limit or 50), 500))
    page_offset = max(0, int(offset or 0))
    results = _ms.search_messages(
        q.strip() if q else None, source=source, content_type=type,
        session_id=session_id,
        additional_session_ids=[overlay_session_id] if overlay_session_id else None,
        limit=page_limit + 1,
        offset=page_offset,
    )
    has_more = len(results) > page_limit
    if has_more:
        results = results[:page_limit]
    navigation = _message_session_navigation()
    for result in results:
        result["navigation"] = navigation.get(result.get("session_id"), {"state": "history"})
    return {
        "query": q, "type": type, "session_id": session_id, "offset": page_offset,
        "limit": page_limit, "count": len(results), "has_more": has_more,
        "next_offset": page_offset + len(results), "results": results,
    }

# --- Usage ---
import base64 as _b64
import urllib.request as _urllib_request

_CC_CREDENTIALS = _Path.home() / ".claude" / ".credentials.json"
_CODEX_AUTH = _Path.home() / ".codex" / "auth.json"
_USAGE_CACHE: dict = {"cc": None, "cc_ts": 0, "codex": None, "codex_ts": 0}
_USAGE_CACHE_TTL = 30


def _cc_usage() -> dict:
    now = time.time()
    if _USAGE_CACHE["cc"] and now - _USAGE_CACHE["cc_ts"] < _USAGE_CACHE_TTL:
        return _USAGE_CACHE["cc"]
    try:
        creds = json.loads(_CC_CREDENTIALS.read_text(encoding="utf-8"))
        oauth = creds.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        sub_type = oauth.get("subscriptionType")
        tier = oauth.get("rateLimitTier")
        if not token:
            return {"ok": False, "error": "no OAuth token"}
        req = _urllib_request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            },
        )
        with _urllib_request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        result = {
            "ok": True,
            "provider": "claude",
            "plan": sub_type,
            "tier": tier,
            "windows": {},
            "extra_usage": data.get("extra_usage"),
        }
        label_map = {
            "five_hour": "5 小时",
            "seven_day": "7 天总量",
            "seven_day_opus": "7 天 Opus",
            "seven_day_sonnet": "7 天 Sonnet",
        }
        for key, label in label_map.items():
            w = data.get(key)
            if w and isinstance(w, dict) and w.get("utilization") is not None:
                result["windows"][key] = {
                    "label": label,
                    "utilization": w["utilization"],
                    "resets_at": w.get("resets_at"),
                }
        _USAGE_CACHE["cc"] = result
        _USAGE_CACHE["cc_ts"] = now
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


_CODEX_SESSIONS_DIR = _Path.home() / ".codex" / "sessions"


def _codex_latest_rate_limits() -> dict | None:
    """Read the newest Codex rate_limits snapshot from local rollout JSONLs."""
    try:
        jsonls = sorted(_CODEX_SESSIONS_DIR.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for jf in jsonls[:50]:
        try:
            with open(jf, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                tail = min(size, 8 * 1024 * 1024)
                f.seek(size - tail)
                lines = f.read().decode("utf-8", errors="replace").splitlines()
            for line in reversed(lines):
                if '"token_count"' not in line or '"rate_limits"' not in line:
                    continue
                obj = json.loads(line)
                payload = obj.get("payload") or {}
                if payload.get("type") == "token_count" and payload.get("rate_limits"):
                    return {
                        "rate_limits": payload["rate_limits"],
                        "timestamp": obj.get("timestamp"),
                        "jsonl": str(jf),
                    }
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _codex_usage() -> dict:
    now = time.time()
    if _USAGE_CACHE["codex"] and now - _USAGE_CACHE["codex_ts"] < _USAGE_CACHE_TTL:
        return _USAGE_CACHE["codex"]
    try:
        auth = json.loads(_CODEX_AUTH.read_text(encoding="utf-8"))
        tokens = auth.get("tokens") or {}
        id_token = tokens.get("id_token") or ""
        parts = id_token.split(".")
        if len(parts) < 2:
            return {"ok": False, "error": "invalid JWT"}
        payload = parts[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        claims = json.loads(_b64.urlsafe_b64decode(payload))
        auth_claims = claims.get("https://api.openai.com/auth") or {}
        snapshot = _codex_latest_rate_limits()
        rl = (snapshot or {}).get("rate_limits") or {}
        plan = rl.get("plan_type") or auth_claims.get("chatgpt_plan_type")
        updated_at = (snapshot or {}).get("timestamp")
        stale = False
        if updated_at:
            try:
                stale = (time.time() - datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp()) > 15 * 60
            except ValueError:
                stale = False
        result = {
            "ok": True,
            "provider": "codex",
            "plan": plan,
            "windows": {},
            "updated_at": updated_at,
            "stale": stale,
        }
        if rl:
            if rl.get("primary"):
                p = rl["primary"]
                result["windows"]["primary"] = {
                    "label": "5 小时",
                    "utilization": p.get("used_percent", 0),
                    "resets_at": p.get("resets_at"),
                }
            if rl.get("secondary"):
                s = rl["secondary"]
                result["windows"]["secondary"] = {
                    "label": "7 天",
                    "utilization": s.get("used_percent", 0),
                    "resets_at": s.get("resets_at"),
                }
        _USAGE_CACHE["codex"] = result
        _USAGE_CACHE["codex_ts"] = now
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/usage")
def api_usage(_=Depends(require_auth)):
    return {"claude": _cc_usage(), "codex": _codex_usage()}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
