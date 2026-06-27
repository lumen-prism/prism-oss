"""Terminal/tmux session manager for the Code tab."""

import asyncio
import base64
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# os imported above

LOG_DIR = Path("/var/tmp/cc_terminal_logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LIVE_STATE_PATH = LOG_DIR / "live_sessions.json"
CODEX_SESSION_MAP_PATH = LOG_DIR / "codex_session_map.json"
OPENCODE_DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
OPENCODE_CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.json"
OPENCODE_MODEL_WINDOWS_PATH = Path(__file__).resolve().parent / "chat_data" / "opencode_model_windows.json"
OPENCODE_SESSION_ARG_RE = re.compile(r"(?:^|\s)(?:-s|--session|--session-id)(?:=|\s+)(ses_[A-Za-z0-9]+)")

# Dashboard inflight tool state — written by ~/.claude/hooks/dashboard-inflight.py
# on PreToolUse / cleared on PostToolUse. Read by read_chat_messages_from_jsonl
# so the chat UI can show "正在执行 X" while a tool is actually running.
INFLIGHT_DIR = Path.home() / ".cache" / "dashboard" / "inflight"
INFLIGHT_STALE_SEC = 120
COMPACTION_DIR = Path.home() / ".cache" / "dashboard" / "compaction"
COMPACTION_STALE_SEC = 30 * 60
_CHAT_PARSE_CACHE_MAX = 16
_CHAT_PARSE_CACHE: dict[str, dict] = {}

# Cap rolling log per session
MAX_LOG_BYTES = 20 * 1024 * 1024  # trim threshold; trim keeps last MAX//2 = 10 MB

# tmux session name allow-list pattern (alnum + dash + underscore, 1–32 chars)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
CODEX_SESSION_RE = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


def _extract_opencode_session_id(args: str) -> Optional[str]:
    if not args:
        return None
    match = OPENCODE_SESSION_ARG_RE.search(args)
    return match.group(1) if match else None


def _process_args(pid: int) -> str:
    try:
        r = _run(["ps", "-p", str(pid), "-o", "args="], timeout=3)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _read_json_file(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _positive_int(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _window_from_model_config(config: dict) -> Optional[int]:
    for key in (
        "context",
        "contextWindow",
        "context_window",
        "contextLength",
        "context_length",
        "maxContextTokens",
        "max_context_tokens",
        "maxInputTokens",
        "max_input_tokens",
    ):
        value = config.get(key)
        if isinstance(value, dict):
            for nested_key in ("tokens", "input", "total"):
                n = _positive_int(value.get(nested_key))
                if n:
                    return n
        n = _positive_int(value)
        if n:
            return n
    return None


def _window_from_mapping(mapping: dict, provider_id: str, model_id: str) -> Optional[int]:
    keys = [f"{provider_id}/{model_id}" if provider_id else "", model_id]
    for key in keys:
        if not key:
            continue
        value = mapping.get(key)
        n = _positive_int(value)
        if n:
            return n
        if isinstance(value, dict):
            n = _window_from_model_config(value)
            if n:
                return n
    provider_entry = mapping.get(provider_id) if provider_id else None
    if isinstance(provider_entry, dict):
        value = provider_entry.get(model_id)
        n = _positive_int(value)
        if n:
            return n
        if isinstance(value, dict):
            n = _window_from_model_config(value)
            if n:
                return n
    return None


def _opencode_model_window(provider_id: str, model_id: str, model_config: Optional[dict] = None) -> int:
    if model_config:
        n = _window_from_model_config(model_config)
        if n:
            return n

    dashboard_mapping = _read_json_file(OPENCODE_MODEL_WINDOWS_PATH) or {}
    n = _window_from_mapping(dashboard_mapping, provider_id, model_id)
    if n:
        return n

    opencode_config = _read_json_file(OPENCODE_CONFIG_PATH) or {}
    provider = ((opencode_config.get("provider") or {}).get(provider_id) or {}) if provider_id else {}
    model_entry = ((provider.get("models") or {}).get(model_id) or {}) if isinstance(provider, dict) else {}
    if isinstance(model_entry, dict):
        n = _window_from_model_config(model_entry)
        if n:
            return n

    return 200_000

# Optional display title (UTF-8, freeform) kept in a sidecar alongside the log.
# The session id stays ASCII so tmux/path semantics never break.
TITLE_MAX_LEN = 64

# ANSI escape sequence matcher for stripping color codes from card summaries.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\r")
ANSI_BYTES_RE = re.compile(
    rb"\x1b\][^\x07]*(?:\x07|\x1b\\)|"
    rb"\x1b\[[0-9;?]*[ -/]*[@-~]|"
    rb"\x1b[()][AB012]|\x1b[=>78]|\x0f"
)
CHAT_INPUT_TAG_RE = re.compile(r"<chat-input\b[^>]*/>\s*", re.IGNORECASE)

# CWD: must be an existing dir under user home
HOME = str(Path.home())


def _run(args, timeout=5) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _tmux_sessions_raw() -> list[dict]:
    r = _run(["tmux", "ls", "-F", "#{session_name}|#{session_created}|#{session_attached}|#{session_windows}"])
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 4:
            continue
        out.append({
            "name": parts[0],
            "created": int(parts[1]),
            "attached": parts[2] == "1",
            "windows": int(parts[3]),
        })
    return out


def _pane_info(session: str) -> Optional[dict]:
    r = _run(["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}|#{pane_current_path}|#{pane_current_command}"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    parts = r.stdout.strip().splitlines()[0].split("|")
    if len(parts) < 3:
        return None
    pid = int(parts[0])
    cwd = parts[1] or HOME
    cmd = parts[2]
    procs = _walk_descendants(pid)
    claude_match = next(((p, a) for p, _c, a in procs if "claude" in a and "claude-plugins" not in a.split()[0:1]), None)
    if claude_match is None:
        claude_match = next(((p, a) for p, _c, a in procs if "claude" in a), None)
    claude_pid = claude_match[0] if claude_match else None
    claude_args = claude_match[1] if claude_match else ""
    opencode_candidates = [
        (p, c, a)
        for p, c, a in procs
        if c == "opencode" or a.startswith("opencode") or " opencode" in a or "/opencode" in a
    ]
    opencode_match = next(((p, c, a) for p, c, a in opencode_candidates if c == "opencode"), None)
    if opencode_match is None:
        opencode_match = opencode_candidates[0] if opencode_candidates else None
    opencode_pid = opencode_match[0] if opencode_match else None
    opencode_args = opencode_match[2] if opencode_match else ""
    opencode_session_id = _extract_opencode_session_id(opencode_args)
    if not opencode_session_id:
        for _opid, _ocmd, candidate_args in opencode_candidates:
            opencode_session_id = _extract_opencode_session_id(candidate_args)
            if opencode_session_id:
                break
    if not opencode_session_id:
        opencode_session_id = _extract_opencode_session_id(_process_args(pid))
    # Prefer Codex's native process over the `node /usr/bin/codex` launcher:
    # only the native process keeps the active rollout JSONL open.
    codex_match = next(((p, c, a) for p, c, a in procs if c == "codex"), None)
    if codex_match is None:
        codex_match = next(((p, c, a) for p, c, a in procs if a.startswith("codex") or " codex" in a or "/codex" in a), None)
    codex_pid = codex_match[0] if codex_match else None
    if claude_pid:
        kind = "cc"
    elif codex_pid:
        kind = "codex"
    elif opencode_pid:
        kind = "opencode"
    else:
        kind = "shell"
    return {
        "pane_pid": pid,
        "cwd": cwd,
        "pane_cmd": cmd,
        "claude_pid": claude_pid,
        "claude_args": claude_args,
        "codex_pid": codex_pid,
        "opencode_pid": opencode_pid,
        "opencode_session_id": opencode_session_id,
        "kind": kind,
    }


def _walk_descendants(root_pid: int) -> list[tuple[int, str, str]]:
    """Returns list of (pid, comm, args) for all descendants of root_pid."""
    try:
        r = _run(["ps", "-eo", "pid,ppid,comm,args", "--no-headers"], timeout=3)
        if r.returncode != 0:
            return []
        kids: dict[int, list[tuple[int, str, str]]] = {}
        for line in r.stdout.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) < 4:
                continue
            pid, ppid, comm, args = parts
            kids.setdefault(int(ppid), []).append((int(pid), comm, args))
        result = []
        stack = [root_pid]
        seen = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for kid_pid, kid_comm, kid_args in kids.get(cur, []):
                result.append((kid_pid, kid_comm, kid_args))
                stack.append(kid_pid)
        return result
    except Exception:
        return []


def _codex_proc_started_at(codex_pid: Optional[int]) -> Optional[float]:
    if not codex_pid:
        return None
    try:
        stat_text = Path(f"/proc/{codex_pid}/stat").read_text(encoding="utf-8")
        after_comm = stat_text.rsplit(") ", 1)[1]
        fields = after_comm.split()
        start_ticks = int(fields[19])
        ticks_per_second = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        with open("/proc/stat", "r", encoding="utf-8") as proc_stat:
            for line in proc_stat:
                if line.startswith("btime "):
                    return int(line.split()[1]) + (start_ticks / ticks_per_second)
    except (OSError, IndexError, KeyError, ValueError):
        return None
    return None


def _read_codex_session_map() -> dict:
    try:
        data = json.loads(CODEX_SESSION_MAP_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_codex_session_map(data: dict) -> None:
    try:
        tmp = CODEX_SESSION_MAP_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CODEX_SESSION_MAP_PATH)
    except Exception:
        pass


def _remember_codex_jsonl(session_name: Optional[str], codex_pid: Optional[int], path: Path) -> None:
    if not session_name or not codex_pid or not path:
        return
    started_at = _codex_proc_started_at(codex_pid)
    if not started_at:
        return
    data = _read_codex_session_map()
    data[session_name] = {
        "pid": codex_pid,
        "proc_started_at": started_at,
        "jsonl_path": str(path),
        "updated_at": int(time.time()),
    }
    _write_codex_session_map(data)


def _mapped_codex_jsonl(session_name: Optional[str], codex_pid: Optional[int]) -> Optional[Path]:
    if not session_name or not codex_pid:
        return None
    item = _read_codex_session_map().get(session_name)
    if not isinstance(item, dict):
        return None
    if int(item.get("pid") or 0) != int(codex_pid):
        return None
    started_at = _codex_proc_started_at(codex_pid)
    if not started_at:
        return None
    try:
        if abs(float(item.get("proc_started_at") or 0) - started_at) > 1:
            return None
    except (TypeError, ValueError):
        return None
    path = Path(item.get("jsonl_path") or "")
    if not path.exists():
        return None
    if _codex_jsonl_claimed_by_other_live_session(session_name, codex_pid, path):
        return None
    return path


def _codex_session_meta(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
        record = json.loads(first) if first else {}
    except (OSError, json.JSONDecodeError):
        return None
    if record.get("type") != "session_meta":
        return None
    payload = record.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    ts = payload.get("timestamp") or record.get("timestamp")
    started_at = None
    if isinstance(ts, str):
        try:
            started_at = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            started_at = None
    return {"payload": payload, "started_at": started_at}


def _codex_jsonl_claimed_by_other_live_session(
    session_name: Optional[str],
    codex_pid: Optional[int],
    path: Path,
    path_started_at: Optional[float] = None,
    proc_cwd: Optional[str] = None,
) -> bool:
    if not session_name or not codex_pid or not path:
        return False
    current_started_at = _codex_proc_started_at(codex_pid)
    if not current_started_at:
        return False
    if path_started_at is None:
        meta = _codex_session_meta(path)
        path_started_at = meta.get("started_at") if meta else None
    if path_started_at is None:
        try:
            path_started_at = path.stat().st_mtime
        except OSError:
            return False
    if proc_cwd is None:
        try:
            proc_cwd = os.readlink(f"/proc/{codex_pid}/cwd")
        except OSError:
            proc_cwd = None
    current_delta = abs(path_started_at - current_started_at)
    try:
        sessions = _tmux_sessions_raw()
    except Exception:
        sessions = []
    for session in sessions:
        other_name = session.get("name")
        if not other_name or other_name == session_name:
            continue
        info = _pane_info(other_name) or {}
        if info.get("kind") != "codex":
            continue
        other_pid = info.get("codex_pid")
        if not other_pid or int(other_pid) == int(codex_pid):
            continue
        if proc_cwd and info.get("cwd"):
            try:
                if os.path.realpath(info["cwd"]) != os.path.realpath(proc_cwd):
                    continue
            except OSError:
                continue
        other_started_at = _codex_proc_started_at(other_pid)
        if not other_started_at:
            continue
        other_delta = abs(path_started_at - other_started_at)
        # A rollout whose session_meta timestamp is much closer to another live
        # Codex process should belong to that pane, not an older idle pane in the
        # same cwd. This prevents a new/idle Chat session from showing another
        # session's transcript.
        if other_delta + 5 < current_delta and path_started_at >= other_started_at - 120:
            return True
    return False


def _find_recent_codex_jsonl_for_pid(codex_pid: Optional[int], session_name: Optional[str] = None) -> Optional[Path]:
    """Fallback for Codex versions that do not keep the rollout JSONL fd open.

    Multiple live Codex panes can share the same cwd. Bind the first resolved
    rollout to the tmux session name so a later pane's newer rollout does not
    steal the older pane's Chat view.
    """
    sessions_dir = Path.home() / ".codex" / "sessions"
    proc_started_at = _codex_proc_started_at(codex_pid)
    if not codex_pid or not proc_started_at or not sessions_dir.exists():
        return None
    mapped = _mapped_codex_jsonl(session_name, codex_pid)
    if mapped:
        return mapped
    try:
        proc_cwd = os.readlink(f"/proc/{codex_pid}/cwd")
    except OSError:
        proc_cwd = None
    try:
        candidates = sorted(
            sessions_dir.rglob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:100]
    except OSError:
        return None
    matches = []
    for path in candidates:
        meta = _codex_session_meta(path)
        if not meta:
            continue
        payload = meta["payload"]
        if proc_cwd and payload.get("cwd") and os.path.realpath(payload["cwd"]) != os.path.realpath(proc_cwd):
            continue
        started_at = meta.get("started_at")
        if started_at is None:
            try:
                started_at = path.stat().st_mtime
            except OSError:
                continue
        if started_at < proc_started_at - 300:
            continue
        if _codex_jsonl_claimed_by_other_live_session(
            session_name, codex_pid, path, path_started_at=started_at, proc_cwd=proc_cwd
        ):
            continue
        matches.append((started_at, path))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    path = matches[0][1]
    _remember_codex_jsonl(session_name, codex_pid, path)
    return path


def _find_codex_jsonl(codex_pid: Optional[int], session_name: Optional[str] = None) -> Optional[Path]:
    """Find the active Codex rollout JSONL from the native codex process."""
    if not codex_pid:
        return None
    mapped = _mapped_codex_jsonl(session_name, codex_pid)
    if mapped:
        return mapped
    candidates = [codex_pid]
    candidates.extend(pid for pid, _cmd, _args in _walk_descendants(codex_pid))
    for candidate in candidates:
        try:
            for fd in Path(f"/proc/{candidate}/fd").iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if ".codex/sessions" in target and target.endswith(".jsonl"):
                    path = Path(target)
                    _remember_codex_jsonl(session_name, codex_pid, path)
                    return path
        except OSError:
            continue
    return _find_recent_codex_jsonl_for_pid(codex_pid, session_name=session_name)


def _read_last_codex_usage(jsonl_path: Path) -> Optional[dict]:
    """Return Codex context usage from the latest token_count event.

    Codex rollout JSONL records token counts as event_msg/payload.type
    "token_count". last_token_usage.input_tokens is the prompt size for the
    most recent turn; cached_input_tokens is a subset, so do not add it again.
    """
    if not jsonl_path or not jsonl_path.exists():
        return None
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            tail = min(size, 32 * 1024 * 1024)
            f.seek(size - tail)
            data = f.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    last = None
    model = None
    for ln in reversed(data.splitlines()):
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        payload = obj.get("payload") or {}
        if model is None and obj.get("type") == "turn_context":
            model = payload.get("model")
        if obj.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        last = obj
        break

    if not last:
        return None
    payload = last.get("payload") or {}
    info = payload.get("info") or {}
    usage = info.get("last_token_usage") or {}
    total_usage = info.get("total_token_usage") or {}
    try:
        tokens = int(usage.get("input_tokens") or 0)
        window = int(info.get("model_context_window") or 0)
    except (TypeError, ValueError):
        return None
    if tokens <= 0 or window <= 0:
        return None

    breakdown = {
        "input": tokens,
        "cached_input": int(usage.get("cached_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "reasoning_output": int(usage.get("reasoning_output_tokens") or 0),
        "total": int(usage.get("total_tokens") or 0),
    }
    return {
        "model": model or "codex",
        "session_id": _codex_session_id(jsonl_path),
        "tokens": tokens,
        "window": window,
        "pct": round(tokens / window * 100, 2),
        "breakdown": breakdown,
        "total_usage": total_usage,
        "rate_limits": payload.get("rate_limits") or {},
        "ts": _parse_iso_ts(last.get("timestamp")),
    }


def _codex_session_id(jsonl: Path) -> Optional[str]:
    match = CODEX_SESSION_RE.search(jsonl.name)
    return "codex-" + match.group(1) if match else None


def log_path(session: str) -> Path:
    return LOG_DIR / f"{session}.log"


def title_path(session: str) -> Path:
    return LOG_DIR / f"{session}.title"


def chat_name_path(session: str) -> Path:
    return LOG_DIR / f"{session}.chat_name"


AVATAR_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def find_avatar_path(session: str) -> Optional[Path]:
    if not NAME_RE.match(session):
        return None
    for ext in AVATAR_EXTS:
        p = LOG_DIR / f"{session}.avatar{ext}"
        if p.exists():
            return p
    return None


def get_display_name(session: str) -> Optional[str]:
    if not NAME_RE.match(session):
        return None
    p = title_path(session)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


def set_display_name(session: str, title: str) -> dict:
    if not NAME_RE.match(session):
        return {"ok": False, "error": "invalid name"}
    title = (title or "").strip()
    if len(title) > TITLE_MAX_LEN:
        return {"ok": False, "error": f"title too long (max {TITLE_MAX_LEN} chars)"}
    p = title_path(session)
    try:
        if title:
            p.write_text(title, encoding="utf-8")
        else:
            p.unlink(missing_ok=True)
        return {"ok": True, "name": session, "display_name": title or None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_chat_name(session: str) -> Optional[str]:
    if not NAME_RE.match(session):
        return None
    p = chat_name_path(session)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


def set_chat_name(session: str, name: str) -> dict:
    if not NAME_RE.match(session):
        return {"ok": False, "error": "invalid name"}
    name = (name or "").strip()
    if len(name) > TITLE_MAX_LEN:
        return {"ok": False, "error": f"chat_name too long (max {TITLE_MAX_LEN} chars)"}
    p = chat_name_path(session)
    try:
        if name:
            p.write_text(name, encoding="utf-8")
        else:
            p.unlink(missing_ok=True)
        return {"ok": True, "name": session, "chat_name": name or None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def save_avatar(session: str, data: bytes, ext: str) -> dict:
    if not NAME_RE.match(session):
        return {"ok": False, "error": "invalid name"}
    ext = (ext or "").lower()
    if not ext.startswith("."):
        ext = "." + ext
    if ext not in AVATAR_EXTS:
        return {"ok": False, "error": f"unsupported extension {ext}"}
    if len(data) > 5 * 1024 * 1024:
        return {"ok": False, "error": "image too large (max 5MB)"}
    # Drop any older avatars with different extensions
    for e in AVATAR_EXTS:
        old = LOG_DIR / f"{session}.avatar{e}"
        if old.exists() and e != ext:
            try: old.unlink()
            except Exception: pass
    p = LOG_DIR / f"{session}.avatar{ext}"
    try:
        p.write_bytes(data)
        return {"ok": True, "name": session, "ext": ext}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def clear_avatar(session: str) -> dict:
    if not NAME_RE.match(session):
        return {"ok": False, "error": "invalid name"}
    for ext in AVATAR_EXTS:
        p = LOG_DIR / f"{session}.avatar{ext}"
        if p.exists():
            try: p.unlink()
            except Exception: pass
    return {"ok": True}


_PIPE_LOCK = threading.Lock()


def ensure_pipe(session: str) -> None:
    """Open the dashboard pipe exactly when a pane does not already have one."""
    if not NAME_RE.match(session):
        return
    path = log_path(session)
    with _PIPE_LOCK:
        state = _run(["tmux", "display-message", "-p", "-t", session, "#{pane_pipe}"], timeout=2)
        if state.returncode != 0 or state.stdout.strip() == "1":
            return
        # Do not use `pipe-pane -o` here: on this tmux it is toggle semantics
        # (an existing pipe is closed), which makes SSE reconnects kill live output.
        _run(["tmux", "pipe-pane", "-t", session, f"cat >> {path}"])


def _summarize_pane(session: str) -> str:
    """Best-effort one-line summary of the session: last non-empty line of pane buffer."""
    if not NAME_RE.match(session):
        return ""
    r = _run(["tmux", "capture-pane", "-p", "-t", session, "-S", "-80"], timeout=3)
    if r.returncode != 0:
        return ""
    raw = ANSI_RE.sub("", r.stdout)
    lines = [ln.strip() for ln in raw.splitlines()]
    # drop empty + drop trailing-cursor-only lines
    candidates = [ln for ln in lines if ln and not re.fullmatch(r"[│┃|>$%#]+\s*", ln)]
    if not candidates:
        return ""
    last = candidates[-1]
    # collapse interior whitespace, trim to ~140 chars
    last = re.sub(r"\s+", " ", last)
    if len(last) > 140:
        last = last[:140] + "…"
    return last


CLAUDE_HOME = Path.home() / ".claude"


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _claude_session_meta_for_pid(claude_pid: Optional[int]) -> dict:
    if not claude_pid:
        return {}
    out: dict = {}
    meta = CLAUDE_HOME / "sessions" / f"{claude_pid}.json"
    if meta.exists():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            sid = data.get("sessionId")
            cwd = data.get("cwd")
            if isinstance(sid, str) and _UUID_RE.fullmatch(sid):
                out["session_id"] = sid
            if isinstance(cwd, str) and cwd:
                out["cwd"] = cwd
        except Exception:
            pass
    if not out.get("cwd"):
        try:
            out["cwd"] = os.readlink(f"/proc/{claude_pid}/cwd")
        except OSError:
            pass
    if not out.get("session_id"):
        try:
            fd_dir = Path(f"/proc/{claude_pid}/fd")
            tasks_prefix = str(CLAUDE_HOME / "tasks") + "/"
            for fd in fd_dir.iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith(tasks_prefix):
                    m = _UUID_RE.search(target)
                    if m:
                        out["session_id"] = m.group(0)
                        break
        except OSError:
            pass
    return out


def _claude_jsonl_for_pid(claude_pid: Optional[int]) -> Optional[Path]:
    """Map a running claude-code PID to its conversation JSONL file.

    Preferred: ~/.claude/sessions/<PID>.json (written at startup).
    Fallback: some CC entry points (e.g. /tui fullscreen re-exec) skip writing
    it. Recover the sessionId by scanning /proc/<PID>/fd/ for the per-session
    tasks dir CC holds open (`~/.claude/tasks/<sessionId>`) — that uniquely
    identifies the JSONL even when multiple CC processes share a cwd.
    """
    if not claude_pid:
        return None
    meta = _claude_session_meta_for_pid(claude_pid)
    sid = meta.get("session_id")
    cwd = meta.get("cwd")
    if not sid or not cwd:
        return _find_recent_claude_jsonl_for_pid(claude_pid)
    p = CLAUDE_HOME / "projects" / cwd.replace("/", "-") / f"{sid}.jsonl"
    if p.exists():
        return p
    return _find_recent_claude_jsonl_for_pid(claude_pid)


def _claude_jsonl_started_at(path: Path) -> Optional[float]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                obj = json.loads(line)
                ts = obj.get("timestamp")
                if isinstance(ts, str):
                    return _parse_iso_ts(ts)
                return None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _find_recent_claude_jsonl_for_pid(claude_pid: Optional[int]) -> Optional[Path]:
    """Fallback when ~/.claude/sessions/<pid>.json exists but is empty.

    Some Claude launches leave the per-pid metadata file at 0 bytes. In that
    case, map the process to a same-cwd project JSONL that started shortly after
    the process did. This keeps new sessions from showing an empty Chat pane.
    """
    if not claude_pid:
        return None
    try:
        proc_started_at = os.stat(f"/proc/{claude_pid}").st_ctime
        cwd = os.readlink(f"/proc/{claude_pid}/cwd")
    except OSError:
        return None
    project_dir = CLAUDE_HOME / "projects" / cwd.replace("/", "-")
    if not project_dir.exists():
        return None
    matches = []
    try:
        candidates = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:80]
    except OSError:
        return None
    for path in candidates:
        started_at = _claude_jsonl_started_at(path)
        if started_at is None:
            continue
        if started_at < proc_started_at - 300:
            continue
        matches.append((started_at, path))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    return matches[0][1]


def _parse_iso_ts(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


_CHANNEL_WRAP_RE = re.compile(
    r"<channel\s+[^>]*>\s*(?P<inner>.*?)\s*</channel>",
    re.DOTALL | re.IGNORECASE,
)
_INBOX_DIR = Path(os.path.expanduser(os.environ.get("PRISM_INBOX_DIR", str(Path.home() / ".local" / "share" / "prism" / "inbox"))))
_IMAGE_PATH_RE = re.compile(
    r"@((?:/tmp/dashboard-uploads/[^\s]+|" + re.escape(str(_INBOX_DIR)) + r"/[^\s]+)\.(?:jpe?g|png|gif|webp|heic))",
    re.IGNORECASE,
)
_IMAGE_ATTR_RE = re.compile(
    r'image_path="(' + re.escape(str(_INBOX_DIR)) + r'/[^"]+\.(?:jpe?g|png|gif|webp|heic))"',
    re.IGNORECASE,
)
_CHAT_INPUT_WRAP_RE = re.compile(r'<chat-input\s+sent_at="[^"]+">\s*(?P<inner>.*?)\s*</chat-input>', re.DOTALL | re.IGNORECASE)
_CHAT_INPUT_PREFIX_RE = re.compile(r'<chat-input\b[^>]*/>\s*', re.IGNORECASE)
_CHAT_INPUT_SENT_AT_RE = re.compile(r'<chat-input\b[^>]*\bsent_at="(?P<ts>[^"]+)"[^>]*/>', re.IGNORECASE)
_PRISM_DATA_DIR = Path(os.path.expanduser(os.environ.get("PRISM_DATA_DIR", "~/.local/share/prism")))
_LATEST_COMPACTION_CACHE = {}
_COMPACTING_PANE_RE = re.compile(r"(?:Compacting|Compacting conversation|Compacting context)", re.IGNORECASE)
_TOOL_USE_SUMMARY = {
    "Read":   ("path",        "📄 Read {}"),
    "Edit":   ("file_path",   "✏️ Edit {}"),
    "Write":  ("file_path",   "✏️ Write {}"),
    "Bash":   ("command",     "⚙️ Bash {}"),
    "Grep":   ("pattern",     "🔍 Grep `{}`"),
    "Glob":   ("pattern",     "🔍 Glob `{}`"),
    "WebFetch": ("url",       "🌐 WebFetch {}"),
    "WebSearch": ("query",    "🌐 Search `{}`"),
    "Task":   ("description", "🤖 Task: {}"),
}


def _clean_user_text(text: str) -> str:
    """Strip transport wrappers while preserving image-attachment references."""
    image_paths = _IMAGE_ATTR_RE.findall(text)
    text = _CHANNEL_WRAP_RE.sub(lambda m: m.group("inner"), text).strip()
    text = _CHAT_INPUT_WRAP_RE.sub(lambda m: m.group("inner"), text).strip()
    text = _CHAT_INPUT_PREFIX_RE.sub("", text).strip()
    if image_paths:
        text = (text + "\n" if text else "") + "\n".join("@" + path for path in image_paths)
    return text


def _chat_message_ts(obj: dict, role: str, content) -> Optional[float]:
    ts = _parse_iso_ts(obj.get("timestamp"))
    if role != "user":
        return ts
    raw_texts = [content] if isinstance(content, str) else [
        block.get("text") or "" for block in (content or [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    for raw_text in raw_texts:
        match = _CHAT_INPUT_SENT_AT_RE.search(raw_text)
        if match:
            return _parse_iso_ts(match.group("ts")) or ts
    return ts


def _text_to_blocks(text: str, session: str) -> list[dict]:
    """Split a text string into a list of {type:text} and {type:image} blocks,
    detecting @-mentioned image paths under /tmp/dashboard-uploads."""
    blocks = []
    cursor = 0
    for m in _IMAGE_PATH_RE.finditer(text):
        if m.start() > cursor:
            chunk = text[cursor:m.start()].strip()
            if chunk:
                blocks.append({"type": "text", "text": chunk})
        path = m.group(1)
        fname = os.path.basename(path)
        src = f"/api/sessions/{session}/uploads/{fname}"
        blocks.append({
            "type": "image",
            "src": src,
            "fname": fname,
        })
        cursor = m.end()
    tail = text[cursor:].strip()
    if tail:
        blocks.append({"type": "text", "text": tail})
    if not blocks:
        blocks.append({"type": "text", "text": text})
    return blocks


def _tool_use_brief(block: dict) -> str:
    name = block.get("name") or "Tool"
    inp = block.get("input") or {}
    if name == "AskUserQuestion":
        qs = inp.get("questions") or []
        if len(qs) == 1:
            head = (qs[0].get("header") or qs[0].get("question") or "").strip()
            if len(head) > 40:
                head = head[:40] + "…"
            return f"❓ 问你：{head}" if head else "❓ 问你一个问题"
        if len(qs) > 1:
            return f"❓ 问你 {len(qs)} 个问题"
        return "❓ AskUserQuestion"
    spec = _TOOL_USE_SUMMARY.get(name)
    if spec:
        key, tmpl = spec
        val = inp.get(key, "")
        if isinstance(val, str):
            val = val.strip()
            if len(val) > 80:
                val = val[:80] + "…"
        return tmpl.format(val)
    return f"🔧 {name}"


def _extract_blocks(obj: dict, session: str, tool_done_ids: set, ask_answers: Optional[dict] = None) -> Optional[Tuple[str, list[dict], Optional[float]]]:
    """Return (role, blocks, ts) for a JSONL entry, or None to skip. Block types:
      - {type:'text', text}
      - {type:'image', src, fname}
      - {type:'tool_group', tools: [{id, name, summary, done}]}  (consecutive tool_use cluster)
    """
    if obj.get("type") not in ("user", "assistant") or obj.get("isSidechain"):
        return None
    role = obj.get("type")
    content = (obj.get("message") or {}).get("content")
    if obj.get("isCompactSummary") and isinstance(content, str):
        return "system", [{
            "type": "compaction", "status": "done", "summary": content,
            "metadata": obj.get("_compact_metadata") or {},
        }], _parse_iso_ts(obj.get("timestamp")), obj.get("uuid")

    blocks: list[dict] = []
    pending_group: Optional[dict] = None

    def flush_group():
        nonlocal pending_group
        if pending_group is not None:
            blocks.append(pending_group)
            pending_group = None

    if isinstance(content, str):
        cleaned = _clean_user_text(content) if role == "user" else content.strip()
        if not cleaned:
            return None
        if cleaned.startswith(("<command-", "<bash-", "<local-command-")) or cleaned.startswith("[Request interrupted"):
            return None
        blocks = _text_to_blocks(cleaned, session)
    elif isinstance(content, list):
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        if "tool_result" in kinds:
            return None
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                flush_group()
                txt = (b.get("text") or "").strip()
                if not txt:
                    continue
                if role == "user":
                    txt = _clean_user_text(txt)
                if not txt:
                    continue
                if txt.startswith(("<command-", "<bash-", "<local-command-")) or txt.startswith("[Request interrupted"):
                    continue
                blocks.extend(_text_to_blocks(txt, session))
            elif t == "thinking" and role == "assistant":
                flush_group()
                thinking = (b.get("thinking") or "").strip()
                if thinking:
                    blocks.append({"type": "thinking", "text": thinking, "done": True})
            elif t == "tool_use" and role == "assistant":
                if pending_group is None:
                    pending_group = {"type": "tool_group", "tools": []}
                tid = b.get("id")
                tool_entry = {
                    "id":      tid,
                    "name":    b.get("name") or "Tool",
                    "summary": _tool_use_brief(b),
                    "done":    bool(tid and tid in tool_done_ids),
                }
                tool_name = b.get("name") or ""
                inp = b.get("input") or {}
                if tool_name in ("Write", "Edit", "write", "edit", "NotebookEdit"):
                    fp = inp.get("file_path") or inp.get("path") or ""
                    if fp:
                        tool_entry["file"] = {
                            "path": fp,
                            "name": os.path.basename(fp),
                            "action": tool_name.lower(),
                        }
                elif tool_name == "AskUserQuestion":
                    ask_card = {
                        "questions": inp.get("questions") or [],
                    }
                    if ask_answers and tid in ask_answers:
                        ask_card["answers"] = ask_answers[tid].get("answers") or {}
                        ask_card["rejected"] = bool(ask_answers[tid].get("rejected"))
                    tool_entry["ask"] = ask_card
                pending_group["tools"].append(tool_entry)
            elif t == "image" and role == "user":
                # Skip embedded base64 images for now — the @-path flow covers our uploads.
                pass
        flush_group()
    else:
        return None

    if not blocks:
        return None
    return role, blocks, _chat_message_ts(obj, role, content), obj.get("uuid")


def _merge_assistant_process_messages(messages: list[dict]) -> list[dict]:
    """Roll up consecutive assistant messages whose blocks are *only*
    tool_group/thinking into a single synthetic message holding one
    process_group block. This is the message-level companion of the
    in-message tool_group merge — Claude Code's transcript emits each
    thinking / tool_use as its own assistant entry, so intra-message
    merging alone never sees the tool+think interleave.

    Run only collapses when it actually mixes tool_group AND thinking;
    pure-tool or pure-thinking runs stay untouched so the existing
    "使用 N 个工具" / single thinking strip can render as before.

    The first message's ts / source_uuid are preserved on the synthetic
    message so anchor / scroll-to-message stays usable."""
    out: list[dict] = []
    buf: list[dict] = []

    def is_process_only(msg: dict) -> bool:
        if msg.get("role") != "assistant":
            return False
        blocks = msg.get("blocks") or []
        if not blocks:
            return False
        return all(b.get("type") in ("tool_group", "thinking") for b in blocks)

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        all_blocks: list[dict] = []
        for m in buf:
            all_blocks.extend(m.get("blocks") or [])
        tool_count = 0
        think_count = 0
        seed = ""
        for b in all_blocks:
            if b.get("type") == "tool_group":
                tool_count += len(b.get("tools") or [])
                if not seed:
                    tools = b.get("tools") or []
                    if tools and tools[0].get("id"):
                        seed = tools[0]["id"]
            elif b.get("type") == "thinking":
                think_count += 1
                if not seed:
                    seed = (b.get("text") or "")[:24]
        if tool_count and think_count and len(buf) >= 2:
            first = buf[0]
            synthetic = {
                "role": "assistant",
                "ts": first.get("ts"),
                "source_uuid": first.get("source_uuid"),
                "blocks": [{
                    "type": "process_group",
                    "children": all_blocks,
                    "toolCount": tool_count,
                    "thinkCount": think_count,
                    "seed": seed or str(first.get("ts") or len(out)),
                }],
            }
            out.append(synthetic)
        else:
            out.extend(buf)
        buf = []

    for m in messages:
        if is_process_only(m):
            buf.append(m)
        else:
            flush()
            out.append(m)
    flush()
    return out


def _merge_cc_turn_tool_groups(messages: list[dict]) -> list[dict]:
    """Merge only *consecutive* tool cards within a turn — any intervening
    thinking/text block separates the groups so the chronological order of
    "tools → think → more tools" survives the render."""
    merged: list[dict] = []
    active_group: Optional[dict] = None
    for message in messages:
        role = message.get("role")
        if role in ("user", "system"):
            active_group = None
        new_blocks = []
        for block in message.get("blocks", []):
            if block.get("type") != "tool_group":
                new_blocks.append(block)
                active_group = None
                continue
            if active_group is None:
                active_group = {
                    "type": "tool_group",
                    "tools": list(block.get("tools", [])),
                }
                new_blocks.append(active_group)
            else:
                active_group["tools"].extend(block.get("tools", []))
        if new_blocks:
            merged.append({**message, "blocks": new_blocks})
    return merged


def _slice_chat_messages(messages: list[dict], limit: int = 200, focus_uuid: Optional[str] = None) -> list[dict]:
    if not limit or len(messages) <= limit:
        return list(messages)
    if focus_uuid:
        target = next((i for i, msg in enumerate(messages) if msg.get("source_uuid") == focus_uuid), None)
        if target is not None:
            start = max(0, target - max(1, limit // 2))
            return list(messages[start:start + limit])
    return list(messages[-limit:])


def _terminal_log_lines(log_file: Path, max_bytes: int = 5 * 1024 * 1024) -> list[str]:
    """Best-effort readable lines from a raw PTY log.

    Some recovered Claude Code sessions have no JSONL pointer because tmux died
    before dashboard could snapshot the Claude pid metadata. The terminal log is
    not a perfect transcript, but it still contains the visible prompt/reply
    markers. This parser is intentionally conservative and only feeds the Chat
    fallback when the normal JSONL path is absent.
    """
    try:
        with open(log_file, "rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
            except OSError:
                pass
            raw = f.read()
    except OSError:
        return []
    text = ANSI_BYTES_RE.sub(b"", raw).decode("utf-8", errors="replace")
    text = text.replace("\r", "\n")
    out: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if line in {"─────────────────────────────────────────────", "───────────────────────────────────────────────────"}:
            continue
        if line.startswith(("⏵", "tmux focus-events", "Welcome back")):
            continue
        if "Auto-update" in line or line.startswith(("newtask?", "new task?", "bypasspermissions")):
            continue
        if line.startswith(("✻", "… ✗ Auto-update")):
            continue
        out.append(line)
    return out


def _terminal_log_messages(log_file: Path, session_label: str, limit: int = 200, focus_uuid: Optional[str] = None) -> list[dict]:
    lines = _terminal_log_lines(log_file)
    messages: list[dict] = []
    current_role: Optional[str] = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_role, current_lines
        if not current_role:
            current_lines = []
            return
        text = "\n".join(current_lines).strip()
        text = CHAT_INPUT_TAG_RE.sub("", text).strip()
        if text:
            messages.append({
                "role": current_role,
                "blocks": _text_to_blocks(text, session_label),
                "ts": None,
                "source_uuid": f"log-{log_file.stem}-{len(messages)}",
                "sid": log_file.stem,
                "recovered_from": "terminal_log",
            })
        current_role = None
        current_lines = []

    def start(role: str, text: str = "") -> None:
        nonlocal current_role, current_lines
        flush()
        current_role = role
        current_lines = []
        text = CHAT_INPUT_TAG_RE.sub("", text).strip()
        if text:
            current_lines.append(text)

    tool_prefixes = (
        "Bash(", "Read(", "Edit(", "Write(", "MultiEdit(", "Grep(",
        "Glob(", "TodoWrite(", "Task(", "WebFetch(", "WebSearch(",
        "Update(", "Create(", "Delete(", "BashOutput(", "KillShell(",
        "Notebook", "LS(",
    )
    def is_output_noise(text: str) -> bool:
        if text in {"OK"}:
            return True
        if "KING_PHRASES" in text or "document.createElement" in text:
            return True
        if re.match(r"^\d{3,5}\b", text):
            return True
        if re.match(r"^[+-]\s*(/\*|<|\.|const\b|let\b|function\b)", text):
            return True
        compact = re.sub(r"\s+", "", text)
        if len(compact) <= 24 and not re.search(r"[\u4e00-\u9fffA-Za-z0-9]{4,}", compact):
            return True
        return False
    for line in lines:
        if is_output_noise(line):
            continue
        if line.startswith("❯"):
            text = line[1:].strip()
            if not text or text.startswith("Try "):
                flush()
                continue
            start("user", text)
            continue
        if line.startswith("●"):
            text = line[1:].strip()
            if text.startswith(tool_prefixes):
                flush()
                continue
            start("assistant", text)
            continue
        if line.startswith("⎿"):
            continue
        if current_role:
            current_lines.append(line)
    flush()

    deduped: list[dict] = []
    seen: set[str] = set()
    seen_fingerprints: set[str] = set()
    for msg in messages:
        text = "\n".join(
            block.get("text") or "" for block in msg.get("blocks", []) if block.get("type") == "text"
        ).strip()
        norm = re.sub(r"\s+", " ", text)
        if not norm:
            continue
        fingerprint = re.sub(r"[\W_]+", "", norm, flags=re.UNICODE)
        if norm in seen:
            continue
        if len(fingerprint) > 80 and any(fingerprint.startswith(old[:80]) or old.startswith(fingerprint[:80]) for old in seen_fingerprints if len(old) > 80):
            continue
        if len(norm) > 80 and any(norm.startswith(old[:80]) or old.startswith(norm[:80]) for old in seen if len(old) > 80):
            continue
        seen.add(norm)
        seen_fingerprints.add(fingerprint)
        deduped.append(msg)
    return _slice_chat_messages(deduped, limit, focus_uuid)


def _latest_archived_log_messages_for_session(session: str, limit: int = 200, focus_uuid: Optional[str] = None) -> list[dict]:
    best: Optional[dict] = None
    for meta in list_archived_sessions():
        if meta.get("name") != session:
            continue
        log_file = Path(meta.get("log_path") or "")
        if not log_file.exists():
            continue
        if best is None or (meta.get("archived_at") or 0) > (best.get("archived_at") or 0):
            best = meta
    if not best:
        return []
    return _terminal_log_messages(Path(best.get("log_path") or ""), session, limit, focus_uuid)


def read_chat_messages_from_jsonl(jsonl: Path, session_label: str, limit: int = 200, focus_uuid: Optional[str] = None) -> list[dict]:
    """Parse a Claude Code conversation JSONL and return the last N displayable
    user/assistant messages in chronological order. Used by both live sessions
    (via list_chat_messages) and archived sessions (which look up the JSONL
    from saved archive metadata rather than a live PID)."""
    try:
        st = jsonl.stat()
        cache_key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    cache = _CHAT_PARSE_CACHE.get(str(jsonl))
    if cache and cache.get("key") == cache_key:
        out = list(cache.get("messages") or [])
        tool_done_ids = set(cache.get("tool_done_ids") or [])
        _LATEST_COMPACTION_CACHE[str(jsonl)] = list(cache.get("compactions") or [])
    else:
        # First pass: collect every tool_use_id that has a matching tool_result.
        # A tool_use without a result yet is treated as "in flight" by the frontend.
        # AskUserQuestion results carry an `answers` dict under toolUseResult, which
        # we stash so the frontend can show what the user picked.
        tool_done_ids: set = set()
        ask_answers: dict[str, dict] = {}
        raw_lines: list[str] = []
        try:
            with open(jsonl, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    raw_lines.append(ln)
                    try:
                        obj = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "user":
                        content = (obj.get("message") or {}).get("content")
                        if isinstance(content, list):
                            for b in content:
                                if isinstance(b, dict) and b.get("type") == "tool_result":
                                    tid = b.get("tool_use_id")
                                    if tid:
                                        tool_done_ids.add(tid)
                                        tur = obj.get("toolUseResult")
                                        if isinstance(tur, dict) and isinstance(tur.get("answers"), dict):
                                            ask_answers[tid] = {
                                                "answers": tur["answers"],
                                                "rejected": bool(b.get("is_error")),
                                            }
                                        elif b.get("is_error"):
                                            # User cancelled / declined — mark as such even
                                            # without structured answers so UI can dim card.
                                            ask_answers[tid] = {"answers": {}, "rejected": True}
        except Exception:
            return []
        out = []
        seen_channel_ids = set()
        compact_metadata = {}
        for ln in raw_lines:
            try:
                boundary = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if boundary.get("type") == "system" and boundary.get("subtype") == "compact_boundary" and boundary.get("uuid"):
                compact_metadata[boundary["uuid"]] = boundary.get("compactMetadata") or {}
        for ln in raw_lines:
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if obj.get("isCompactSummary"):
                obj = dict(obj)
                obj["_compact_metadata"] = compact_metadata.get(obj.get("parentUuid"), {})
            if obj.get("type") == "attachment":
                attachment = obj.get("attachment") or {}
                prompt = attachment.get("prompt") if attachment.get("type") == "queued_command" else None
                if isinstance(prompt, str) and "<channel" in prompt:
                    obj = dict(obj)
                    obj["type"] = "user"
                    obj["message"] = {"content": prompt}
            if obj.get("type") == "user":
                content = (obj.get("message") or {}).get("content")
                if isinstance(content, str) and "<channel" in content:
                    match = re.search(r'message_id="([^"]+)"', content)
                    if match and match.group(1) in seen_channel_ids:
                        continue
                    if match:
                        seen_channel_ids.add(match.group(1))
            m = _extract_blocks(obj, session_label, tool_done_ids, ask_answers)
            if m is None:
                continue
            role, blocks, ts, source_uuid = m
            out.append({"role": role, "blocks": blocks, "ts": ts, "source_uuid": source_uuid, "sid": jsonl.stem})
        out = _merge_cc_turn_tool_groups(out)
        out = _merge_assistant_process_messages(out)
        completed_compactions = [msg for msg in out if any(block.get("type") == "compaction" and block.get("status") == "done" for block in msg.get("blocks", []))]
        _LATEST_COMPACTION_CACHE[str(jsonl)] = completed_compactions
        _CHAT_PARSE_CACHE[str(jsonl)] = {
            "key": cache_key,
            "messages": out,
            "compactions": completed_compactions,
            "tool_done_ids": tool_done_ids,
            "touched": time.time(),
        }
        if len(_CHAT_PARSE_CACHE) > _CHAT_PARSE_CACHE_MAX:
            oldest = sorted(_CHAT_PARSE_CACHE.items(), key=lambda item: item[1].get("touched", 0))[:len(_CHAT_PARSE_CACHE) - _CHAT_PARSE_CACHE_MAX]
            for key, _value in oldest:
                _CHAT_PARSE_CACHE.pop(key, None)
    out = _slice_chat_messages(out, limit, focus_uuid)
    inflight = _read_inflight_for_jsonl(jsonl)
    if inflight is not None:
        # Drop the inflight synthetic message if its tool_use_id is already
        # marked done in the JSONL. Race: tool_result is written before the
        # PostToolUse hook clears the inflight sidecar, so for ~1-2s the same
        # AskUserQuestion would otherwise render twice (done card + live card).
        inflight_tid = None
        for blk in inflight.get("blocks", []):
            if blk.get("type") == "tool_group":
                for t in (blk.get("tools") or []):
                    tid = t.get("id")
                    if tid and tid != "inflight":
                        inflight_tid = tid
                        break
                break
        if not (inflight_tid and inflight_tid in tool_done_ids):
            out.append(inflight)
    return out


def _read_inflight_for_jsonl(jsonl: Path) -> Optional[dict]:
    """Read the inflight state for this session and return a synthetic
    assistant message containing a single pending tool_group, or None if
    there's no fresh entry. Two states are surfaced: 'thinking' (between
    user prompt and first tool, or between tools) and 'tool' (a specific
    tool is running). The session id is the JSONL filename stem (matches
    what Claude Code passes to hooks as session_id)."""
    sid = jsonl.stem
    if not sid:
        return None
    inflight_path = INFLIGHT_DIR / f"{sid}.json"
    try:
        raw = inflight_path.read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    ts = data.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    state = data.get("state") or "tool"
    name_for_stale = data.get("name") or ""
    # AskUserQuestion legitimately blocks for minutes/hours waiting on the
    # user, so the usual 120s stale guard would mask the live card. Its
    # PostToolUse hook clears the sidecar, so we don't need stale protection.
    is_ask = (state == "tool" and name_for_stale == "AskUserQuestion")
    if not is_ask and time.time() - ts > INFLIGHT_STALE_SEC:
        return None
    if state == "thinking":
        name = "thinking"
        summary = "💭 思考中…"
        tool_entry = {
            "id": "inflight",
            "name": name,
            "summary": summary,
            "done": False,
        }
    else:
        name = data.get("name") or "Tool"
        inp = data.get("input") or {}
        fake_block = {"name": name, "input": inp}
        summary = _tool_use_brief(fake_block)
        # If Claude Code's hook surfaced the real tool_use_id, key on it so the
        # chat card survives the swap from inflight sidecar to JSONL-derived
        # state without losing local UI state (toggle, focus advance).
        tool_id = data.get("tool_use_id") if isinstance(data.get("tool_use_id"), str) else "inflight"
        tool_entry = {
            "id": tool_id,
            "name": name,
            "summary": summary,
            "done": False,
        }
        if name == "AskUserQuestion":
            tool_entry["ask"] = {
                "questions": inp.get("questions") or [],
            }
    return {
        "role": "assistant",
        "blocks": [{
            "type": "tool_group",
            "tools": [tool_entry],
        }],
        "ts": float(ts),
    }


def compaction_messages(session: str) -> list[dict]:
    """Return completed compactions for a chat, newest first."""
    info = _pane_info(session) or {}
    if info.get("kind") != "cc":
        return []
    active_jsonl = _claude_jsonl_for_pid(info.get("claude_pid"))
    if active_jsonl is None:
        return []
    candidates = list(_LATEST_COMPACTION_CACHE.get(str(active_jsonl)) or [])
    candidates.sort(key=lambda message: message.get("ts") or 0, reverse=True)
    return candidates


def _compaction_in_progress(session: str, jsonl: Path) -> Optional[dict]:
    """Return a live compaction card from the PreCompact hook marker.

    Claude writes compact_boundary only after compaction finishes, and its pane
    does not consistently expose a progress label. The hook marker supplies the
    missing start event; a newer completed summary clears it automatically.
    """
    marker_path = COMPACTION_DIR / f"{jsonl.stem}.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker_ts = marker.get("ts")
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        marker = None
        marker_ts = None
    if isinstance(marker_ts, (int, float)):
        if time.time() - marker_ts <= COMPACTION_STALE_SEC:
            completed = _LATEST_COMPACTION_CACHE.get(str(jsonl)) or []
            if not any((message.get("ts") or 0) >= marker_ts for message in completed):
                return {
                    "role": "system",
                    "blocks": [{"type": "compaction", "status": "running"}],
                    "ts": float(marker_ts),
                    "source_uuid": "compaction-running",
                }
        try:
            marker_path.unlink(missing_ok=True)
        except OSError:
            pass
    # Preserve the old screen-text fallback for any Claude version that emits it.
    try:
        result = _run(["tmux", "capture-pane", "-p", "-t", session, "-S", "-40"], timeout=3)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    pane = ANSI_RE.sub("", result.stdout or "")
    if not _COMPACTING_PANE_RE.search(pane):
        return None
    return {
        "role": "system",
        "blocks": [{"type": "compaction", "status": "running"}],
        "ts": time.time(),
        "source_uuid": "compaction-running",
    }


def _is_inflight_message(message: dict) -> bool:
    return any(
        block.get("type") == "tool_group" and any(tool.get("id") == "inflight" for tool in block.get("tools", []))
        for block in message.get("blocks", [])
    )


_CC_BREWING_RE = re.compile(r"✻\s+\w+.*\d+s", re.MULTILINE)
# Claude Code interactive menus mark the selected row with ❯ and list options
# as "N. label". One option line carrying the ❯ cursor is the reliable signal
# that the session is BLOCKED on a choice (vs. model output that happens to
# contain a numbered list).
_CHOICE_CURSOR_RE = re.compile(r"^[ \t]*❯[ \t]*\d+\.[ \t]+\S", re.MULTILINE)
_NUM_OPTION_RE = re.compile(r"^[ \t]*❯?[ \t]*(\d+)\.[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_FEEDBACK_RE = re.compile(r"How is Claude doing this session\?", re.IGNORECASE)
# Title heuristic: a short line ending in ? or a known menu heading, just above
# the option block.
_TITLE_HINT_RE = re.compile(r"^[ \t]*([A-Z一-鿿][^?\n]{0,60}\?)[ \t]*$", re.MULTILINE)
_MENU_HEADING_RE = re.compile(r"^[ \t]*(Select [A-Za-z ]+|Switch [a-z ]+)[ \t]*$", re.MULTILINE)


def detect_terminal_prompt(session: str) -> Optional[dict]:
    """Detect if a Claude Code session is blocked on an interactive prompt
    (feedback survey, numbered-choice menu, confirmation) and return metadata
    for the frontend to render a card with action buttons.

    Numbered menus (model switch, effort, tool permission, trust folder, etc.)
    all share the '❯ N. label' format and confirm on a single digit keypress —
    no Enter needed. We scan after the last '✻ …for Ns' brew marker when present
    (avoids false positives from model output), but fall back to the whole pane
    for startup prompts that have no brew marker (e.g. the trust-folder gate)."""
    try:
        result = _run(["tmux", "capture-pane", "-p", "-t", session, "-S", "-30"], timeout=3)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    pane = ANSI_RE.sub("", result.stdout or "")
    brews = list(_CC_BREWING_RE.finditer(pane))
    tail = pane[brews[-1].end():] if brews else pane

    # Feedback survey — single-line digit shortcuts (0 Dismiss / 1-3 rating).
    if _FEEDBACK_RE.search(tail):
        return {
            "type": "feedback",
            "label": "Claude 使用反馈",
            "text": "Claude Code 想收集一次使用反馈，挡住了输入。",
            "actions": [
                {"key": "0", "label": "跳过", "keys": ["0", "Enter"]},
                {"key": "3", "label": "👍 Good", "keys": ["3", "Enter"]},
                {"key": "2", "label": "🤷 Fine", "keys": ["2", "Enter"]},
                {"key": "1", "label": "👎 Bad", "keys": ["1", "Enter"]},
            ],
        }

    # Numbered-choice menu — require the ❯ cursor on an option line.
    if _CHOICE_CURSOR_RE.search(tail):
        seen = {}
        for num, text in _NUM_OPTION_RE.findall(tail):
            if num not in seen:
                # Option name and its description column are separated by a run
                # of 2+ spaces — keep just the name.
                name = re.split(r"\s{2,}", text.strip())[0]
                name = name.strip().rstrip("✔ ").strip()
                seen[num] = name[:36]
        if len(seen) >= 2:
            title = None
            m = _TITLE_HINT_RE.search(tail) or _MENU_HEADING_RE.search(tail)
            if m:
                title = m.group(1).strip()
            actions = [
                {"key": num, "label": f"{num}. {label}", "keys": [num]}
                for num, label in sorted(seen.items(), key=lambda kv: int(kv[0]))
            ]
            actions.append({"key": "esc", "label": "取消", "keys": ["Escape"]})
            return {
                "type": "choice",
                "label": title or "终端需要选择",
                "text": "终端弹出了一个选择菜单，挡住了输入。",
                "actions": actions,
            }
    return None


def _wait_for_ask_review(session: str, timeout: float = 0.9) -> bool:
    """Poll the pane until the AskUserQuestion review screen is showing.

    After the last question is answered the TUI transitions to a
    "Review your answers … 1. Submit answers / 2. Cancel" screen. Sending the
    confirm key before that transition lands it on the wrong screen and submits
    the wrong answer — the root of the "最后确认不是我点的选项" bug. Waiting for
    the review text makes the confirm deterministic instead of a fixed-delay race.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = _run(["tmux", "capture-pane", "-p", "-t", session, "-S", "-24"], timeout=3)
        if r.returncode == 0:
            pane = ANSI_RE.sub("", r.stdout or "")
            if "Submit answers" in pane or "Ready to submit" in pane:
                return True
        time.sleep(0.08)
    return False


def send_terminal_keys(session: str, keys: list, pace: float = 0.2) -> dict:
    """Send a sequence of keys to a tmux session.

    Each element is one of:
      - str: a tmux key name sent as-is ("1", "Enter", "Tab", "Escape", "Right").
      - {"text": "..."}: literal text via `send-keys -l` (preserves spaces /
        Chinese / punctuation — for AskUserQuestion "Type something" answers).
      - {"submit_after_review": true}: wait for the AskUserQuestion review screen,
        then press "1". Removes the confirm-before-transition race.

    `pace` is the inter-key delay; the Ask path passes a tighter value than the
    0.2s default used by blocking-prompt replies.
    """
    if not NAME_RE.match(session):
        return {"ok": False, "error": "invalid session name"}
    for key in keys:
        if isinstance(key, dict):
            if "text" in key:
                txt = str(key.get("text") or "")
                if txt:
                    # `--` guards against a leading dash being read as a flag.
                    r = _run(["tmux", "send-keys", "-t", session, "-l", "--", txt])
                    if r.returncode != 0:
                        return {"ok": False, "error": r.stderr.strip() or "send-keys failed"}
            elif key.get("submit_after_review"):
                if not _wait_for_ask_review(session):
                    return {"ok": False, "error": "review screen did not appear"}
                r = _run(["tmux", "send-keys", "-t", session, "1"])
                if r.returncode != 0:
                    return {"ok": False, "error": r.stderr.strip() or "send-keys failed"}
            else:
                return {"ok": False, "error": f"unknown key spec: {key!r}"}
        else:
            r = _run(["tmux", "send-keys", "-t", session, str(key)])
            if r.returncode != 0:
                return {"ok": False, "error": r.stderr.strip() or "send-keys failed"}
        time.sleep(pace)
    return {"ok": True}


_CLAUDE_AFTER_PROMPT_OUTPUT_RE = re.compile(
    r"\n\s*(?:Thought for\b|●|✻|•\s|╭|─{3,}[^─\s])",
    re.MULTILINE,
)


def _capture_plain_pane(session: str, start: str = "-120") -> Optional[str]:
    try:
        result = _run(["tmux", "capture-pane", "-p", "-t", session, "-S", start], timeout=3)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return ANSI_RE.sub("", result.stdout or "")


def _last_claude_prompt_tail(pane: str) -> Optional[str]:
    if not pane:
        return None
    idx = pane.rfind("❯")
    if idx < 0:
        return None
    return pane[idx + 1:]


def _tail_first_line_text(tail: str) -> str:
    line = (tail or "").splitlines()[0] if tail is not None else ""
    return line.replace("\xa0", " ").strip()


def claude_prompt_state(session: str) -> dict:
    """Return whether a Claude Code prompt is visibly ready for injected input.

    Claude Code can accept a tmux paste while it is still finishing the previous
    turn, leaving the pasted text visibly parked in the input box. The dashboard
    uses this screen-level state as a guard in addition to transcript polling,
    because the transcript can lag behind the TUI.
    """
    info = _pane_info(session) or {}
    if info.get("kind") != "cc":
        return {"ready": True, "reason": "non_claude"}
    pane = _capture_plain_pane(session)
    if pane is None:
        return {"ready": False, "reason": "capture_failed"}
    tail = _last_claude_prompt_tail(pane)
    if tail is None:
        return {"ready": False, "reason": "no_prompt"}
    if _CHOICE_CURSOR_RE.search(tail):
        return {"ready": False, "reason": "choice_prompt"}
    first_line = _tail_first_line_text(tail)
    if first_line:
        if _CLAUDE_AFTER_PROMPT_OUTPUT_RE.search(tail):
            return {"ready": False, "reason": "submitted_prompt_history"}
        return {"ready": False, "reason": "input_pending"}
    return {"ready": True, "reason": "ready"}


def _wait_for_claude_prompt_ready(session: str, timeout: float = 18.0) -> dict:
    deadline = time.monotonic() + max(0.0, timeout)
    last = {"ready": False, "reason": "not_checked"}
    while True:
        state = claude_prompt_state(session)
        last = state
        if state.get("ready"):
            return state
        if time.monotonic() >= deadline:
            return last
        time.sleep(0.25)


def list_chat_messages(session: str, limit: int = 200, focus_uuid: Optional[str] = None) -> list[dict]:
    """Parse live Claude JSONL into chat-render blocks for a session."""
    info = _pane_info(session) or {}
    if info.get("kind") == "codex":
        jsonl = _find_codex_jsonl(info.get("codex_pid"), session_name=session)
        if jsonl is None:
            return []
        try:
            import message_store as _ms
            return _ms.read_codex_chat_messages(jsonl, limit, focus_uuid=focus_uuid)
        except Exception:
            return []
    if info.get("kind") == "opencode":
        try:
            messages = read_opencode_chat_messages(limit, focus_uuid=focus_uuid,
                                                   pane_cwd=info.get("cwd"),
                                                   session_id=info.get("opencode_session_id"),
                                                   opencode_pid=info.get("opencode_pid"))
            inflight = _opencode_inflight_message(pane_cwd=info.get("cwd"),
                                                   session_id=info.get("opencode_session_id"),
                                                   opencode_pid=info.get("opencode_pid"))
            if inflight:
                messages.append(inflight)
            return messages
        except Exception:
            return []
    if info.get("kind") != "cc":
        return []
    jsonl = _claude_jsonl_for_pid(info.get("claude_pid"))
    if jsonl is None:
        return _latest_archived_log_messages_for_session(session, limit, focus_uuid)
    messages = read_chat_messages_from_jsonl(jsonl, session, limit, focus_uuid=focus_uuid)
    running = _compaction_in_progress(session, jsonl)
    if running:
        messages = [message for message in messages if not _is_inflight_message(message)]
        messages.append(running)
    return messages


def _last_message_from_jsonl(jsonl_path: Path) -> Tuple[Optional[str], Optional[float]]:
    """Tail the conversation JSONL and return (text, unix_ts) of the last
    displayable message from either side of the conversation, so the card
    preview reflects whatever actually happened — not the pane's idle redraws.

    Skips sidechain (subagent) turns, tool calls/results, thinking blocks, and
    synthesized user turns like command outputs or request-interrupted notices.
    """
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            tail = min(size, 128 * 1024)
            f.seek(size - tail)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
    except Exception:
        return None, None
    last_visible = (None, None)
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if obj.get("type") not in ("user", "assistant") or obj.get("isSidechain"):
            continue
        content = (obj.get("message") or {}).get("content")
        text = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            kinds = {b.get("type") for b in content if isinstance(b, dict)}
            if "tool_result" in kinds:
                continue
            parts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
            if parts:
                text = " ".join(parts)
        if not text:
            continue
        text = text.strip()
        if not text:
            continue
        if text.startswith("<command-") or text.startswith("<bash-") or text.startswith("<local-command-"):
            continue
        if text.startswith("[Request interrupted"):
            continue
        if obj.get("type") == "user":
            text = _clean_user_text(text)
        last_visible = (text, _parse_iso_ts(obj.get("timestamp")))
    return last_visible


def _last_codex_message_from_jsonl(jsonl_path: Path) -> Tuple[Optional[str], Optional[float]]:
    """Tail a Codex rollout JSONL and return the latest visible user/assistant text."""
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            tail = min(size, 512 * 1024)
            f.seek(size - tail)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
    except Exception:
        return None, None
    last_visible = (None, None)
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "response_item":
            continue
        payload = obj.get("payload") or {}
        if payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        want = "input_text" if role == "user" else "output_text"
        parts = []
        for block in payload.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != want:
                continue
            text = (block.get("text") or "").strip()
            if not text:
                continue
            if role == "user" and (text.startswith("<environment_context>") or text.startswith("<permissions")):
                continue
            parts.append(text)
        if not parts:
            continue
        text = "\n".join(parts)
        if role == "user":
            sent_match = _CHAT_INPUT_SENT_AT_RE.search(text)
            ts = _parse_iso_ts(sent_match.group("ts") if sent_match else obj.get("timestamp"))
            text = _clean_user_text(re.sub(r"<chat-input\b[^>]*/>\s*", "", text, flags=re.IGNORECASE).strip())
        else:
            ts = _parse_iso_ts(obj.get("timestamp"))
        text = re.sub(r"<oai-mem-citation>.*?</oai-mem-citation>", "", text, flags=re.DOTALL).strip()
        if text:
            last_visible = (text, ts)
    return last_visible


# ---------------------------------------------------------------------------
# OpenCode chat reader
# ---------------------------------------------------------------------------
# OpenCode stores sessions in a single SQLite database (~/.local/share/opencode/opencode.db).
# Schema: message(id, session_id, data JSON) → part(id, message_id, session_id, data JSON)
# Part types: "text", "reasoning", "tool" (bash/edit/glob/grep/read/task/write), "step-start", "step-finish"

def _find_opencode_session(pane_cwd: Optional[str] = None, opencode_pid: Optional[int] = None,
                           session_id: Optional[str] = None) -> Optional[str]:
    """Find the active OpenCode session ID from the database.

    Heuristic: prefer the most recently updated non-archived session whose
    directory matches *pane_cwd*, then fall back to the overall newest one.
    If opencode_pid is provided, try to match by process start time.
    """
    if session_id:
        return session_id
    db = OPENCODE_DB_PATH
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            rows = conn.execute(
                "SELECT id, directory, time_created FROM session "
                "WHERE time_archived IS NULL ORDER BY time_updated DESC"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    if not rows:
        return None

    # If we have the opencode PID, try to match by process start time
    if opencode_pid:
        try:
            import psutil
            proc = psutil.Process(opencode_pid)
            create_time = proc.create_time()
            # Convert to milliseconds (opencode uses ms timestamps)
            create_time_ms = int(create_time * 1000)
            # Find sessions created within 30 seconds of process start
            for sid, directory, time_created in rows:
                if abs(time_created - create_time_ms) < 30000:
                    return sid
        except Exception:
            pass

    if pane_cwd:
        for sid, directory, _ in rows:
            if directory == pane_cwd:
                return sid
    return rows[0][0]


def read_opencode_chat_messages(limit: int = 200, focus_uuid: Optional[str] = None,
                                pane_cwd: Optional[str] = None,
                                session_id: Optional[str] = None,
                                opencode_pid: Optional[int] = None) -> list[dict]:
    """Read OpenCode session from SQLite and return messages in the standard
    block format consumed by the Chat UI (same shape as CC / Codex readers).

    OpenCode's part types → frontend block types:
      text       → {type:'text', text}
      reasoning  → {type:'thinking', text}
      tool       → accumulated into {type:'tool_group', tools:[...]}
      step-start → (ignored, boundary marker)
      step-finish→ flushes pending tool_group, marks tools done
    """
    db = OPENCODE_DB_PATH
    if not db.exists():
        return []
    if not session_id:
        session_id = _find_opencode_session(pane_cwd, opencode_pid)
    if not session_id:
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            msg_rows = conn.execute(
                "SELECT id, time_created, data FROM message "
                "WHERE session_id = ? ORDER BY time_created",
                (session_id,),
            ).fetchall()
            if not msg_rows:
                return []
            all_msg_ids = [r[0] for r in msg_rows]
            placeholders = ",".join("?" * len(all_msg_ids))
            part_rows = conn.execute(
                f"SELECT message_id, data FROM part "
                f"WHERE message_id IN ({placeholders}) ORDER BY rowid",
                all_msg_ids,
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    parts_by_msg: dict[str, list[dict]] = {}
    for mid, pdata in part_rows:
        try:
            parts_by_msg.setdefault(mid, []).append(json.loads(pdata))
        except (json.JSONDecodeError, TypeError):
            continue

    # Track which tool callIDs have results (done) within each message.
    done_by_msg: dict[str, set] = {}
    for mid, parts in parts_by_msg.items():
        done: set = set()
        for p in parts:
            pt = p.get("type")
            if pt == "step-finish":
                for q in parts:
                    if q.get("type") == "tool":
                        call_id = q.get("callID")
                        if call_id:
                            done.add(call_id)
            elif pt == "tool":
                state = p.get("state") or {}
                if state.get("status") == "completed":
                    call_id = p.get("callID")
                    if call_id:
                        done.add(call_id)
        done_by_msg[mid] = done

    def _tool_summary(p: dict) -> str:
        """Generate a brief summary for a tool call."""
        tool = p.get("tool", "")
        state = p.get("state") or {}
        inp = state.get("input") or {}
        if tool == "bash":
            return inp.get("command") or inp.get("description") or "bash"
        if tool in ("read",):
            return inp.get("filePath") or inp.get("path") or tool
        if tool in ("write", "edit"):
            return inp.get("filePath") or inp.get("path") or tool
        if tool in ("glob", "grep"):
            return inp.get("pattern") or tool
        if tool == "task":
            return inp.get("description") or inp.get("prompt", "")[:60] or "subagent"
        if tool == "todowrite":
            return "todo list"
        return inp.get("description") or tool

    out: list[dict] = []
    for msg_id, msg_ts_raw, msg_data_str in msg_rows:
        try:
            msg_data = json.loads(msg_data_str)
        except (json.JSONDecodeError, TypeError):
            continue
        role = msg_data.get("role")
        if role not in ("user", "assistant"):
            continue
        parts = parts_by_msg.get(msg_id, [])
        done_ids = done_by_msg.get(msg_id, set())
        blocks: list[dict] = []
        pending_group: Optional[dict] = None

        def _flush_group():
            nonlocal pending_group
            if pending_group is not None:
                blocks.append(pending_group)
                pending_group = None

        for p in parts:
            pt = p.get("type")
            if pt == "text":
                _flush_group()
                text = (p.get("text") or "").strip()
                if role == "user":
                    text = CHAT_INPUT_TAG_RE.sub("", text).strip()
                if text:
                    blocks.append({"type": "text", "text": text})
            elif pt == "reasoning":
                _flush_group()
                text = (p.get("text") or "").strip()
                if text:
                    blocks.append({"type": "thinking", "text": text, "done": True})
            elif pt == "tool":
                call_id = p.get("callID")
                if pending_group is None:
                    pending_group = {"type": "tool_group", "tools": []}
                tool_name = p.get("tool") or "tool"
                tool_entry: dict = {
                    "id": call_id or f"tool-{len(blocks)}",
                    "name": tool_name,
                    "summary": _tool_summary(p),
                    "done": bool(call_id and call_id in done_ids),
                }
                # Add file info for write/edit tools
                state = p.get("state") or {}
                inp = state.get("input") or {}
                if tool_name in ("write", "edit"):
                    fp = inp.get("filePath") or inp.get("path") or ""
                    if fp:
                        tool_entry["file"] = {
                            "path": fp,
                            "name": os.path.basename(fp),
                            "action": tool_name,
                        }
                pending_group["tools"].append(tool_entry)
            elif pt in ("step-start", "step-finish"):
                _flush_group()
            # Other types ignored

        _flush_group()
        if not blocks:
            continue
        ts = msg_ts_raw / 1000.0 if msg_ts_raw else None
        out.append({
            "role": role,
            "blocks": blocks,
            "ts": ts,
            "source_uuid": msg_id,
            "sid": session_id,
        })

    # Merge consecutive assistant messages that share the same parentID
    # into unified turns (OpenCode emits one message per "step" in a turn).
    # All assistant steps in a turn share parentID = the user message ID.
    # User messages are never merged into — they're always standalone.
    merged: list[dict] = []
    msg_parent: dict[str, str] = {}
    for msg_id, _, msg_data_str in msg_rows:
        try:
            md = json.loads(msg_data_str)
        except Exception:
            continue
        pid = md.get("parentID")
        if pid:
            msg_parent[msg_id] = pid

    for item in out:
        uuid = item.get("source_uuid")
        parent_id = msg_parent.get(uuid)
        # Merge assistant→assistant when both share the same parentID (same turn)
        if (item["role"] == "assistant" and parent_id and merged
                and merged[-1]["role"] == "assistant"
                and msg_parent.get(merged[-1].get("source_uuid")) == parent_id):
            merged[-1]["blocks"].extend(item["blocks"])
        else:
            merged.append(item)

    # Trim to limit
    if focus_uuid:
        try:
            idx = next(i for i, m in enumerate(merged) if m.get("source_uuid") == focus_uuid)
            start = max(0, idx - limit // 2)
            return merged[start:start + limit]
        except StopIteration:
            pass
    return merged[-limit:]


def _last_opencode_message(pane_cwd: Optional[str] = None, opencode_pid: Optional[int] = None,
                           session_id: Optional[str] = None) -> Tuple[Optional[str], Optional[float]]:
    """Return (text, ts) of the last user/assistant text in the active OpenCode session."""
    db = OPENCODE_DB_PATH
    if not db.exists():
        return None, None
    session_id = _find_opencode_session(pane_cwd, opencode_pid, session_id=session_id)
    if not session_id:
        return None, None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            rows = conn.execute(
                "SELECT m.id, m.time_created, m.data, p.data "
                "FROM message m "
                "JOIN part p ON p.message_id = m.id "
                "WHERE m.session_id = ? AND p.data LIKE '%\"type\":\"text\"%' "
                "ORDER BY m.time_created DESC LIMIT 5",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None, None
    for _mid, ts_raw, _mdata, pdata in rows:
        try:
            part = json.loads(pdata)
        except Exception:
            continue
        if part.get("type") == "text" and (part.get("text") or "").strip():
            return part["text"].strip(), (ts_raw / 1000.0 if ts_raw else None)
    return None, None


def _opencode_inflight_message(pane_cwd: Optional[str] = None, opencode_pid: Optional[int] = None,
                               session_id: Optional[str] = None) -> Optional[dict]:
    """Check if the OpenCode session is currently processing (thinking/acting).

    Returns a synthetic assistant message with a thinking or tool_group block
    if the latest assistant message is still in-progress, or None if idle.
    """
    db = OPENCODE_DB_PATH
    if not db.exists():
        return None
    session_id = _find_opencode_session(pane_cwd, opencode_pid, session_id=session_id)
    if not session_id:
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            # Get the latest assistant message
            row = conn.execute(
                "SELECT id, time_created FROM message "
                "WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant' "
                "ORDER BY time_created DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            msg_id, ts_raw = row
            # Get its parts
            parts = conn.execute(
                "SELECT data FROM part WHERE message_id = ? ORDER BY rowid",
                (msg_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None

    # Parse parts to check completion state
    has_step_start = False
    has_step_finish = False
    has_text = False
    has_reasoning = False
    last_reasoning = ""
    pending_tool = None

    for (pdata,) in parts:
        try:
            p = json.loads(pdata)
        except Exception:
            continue
        pt = p.get("type")
        if pt == "step-start":
            has_step_start = True
        elif pt == "step-finish":
            has_step_finish = True
            pending_tool = None  # reset for next step
        elif pt == "reasoning":
            has_reasoning = True
            text = (p.get("text") or "").strip()
            if text:
                last_reasoning = text
        elif pt == "text":
            has_text = True
        elif pt == "tool":
            state = p.get("state") or {}
            if state.get("status") != "completed":
                pending_tool = p.get("tool") or "tool"

    # If there's a step-start but no step-finish, the message is in-progress
    if has_step_start and not has_step_finish:
        blocks = []
        if has_reasoning and last_reasoning:
            blocks.append({"type": "thinking", "text": last_reasoning, "done": False})
        if pending_tool:
            blocks.append({"type": "tool_group", "tools": [{
                "id": "inflight",
                "name": pending_tool,
                "summary": f"正在执行 {pending_tool}…",
                "done": False,
            }]})
        if not blocks:
            blocks.append({"type": "thinking", "text": "", "done": False})
        return {
            "role": "assistant",
            "blocks": blocks,
            "ts": ts_raw / 1000.0 if ts_raw else None,
            "source_uuid": f"inflight-{msg_id}",
            "sid": session_id,
        }
    return None


def _read_opencode_usage(pane_cwd: Optional[str] = None, opencode_pid: Optional[int] = None,
                         session_id: Optional[str] = None) -> Optional[dict]:
    """Read token usage from the last assistant message in the OpenCode session.

    The session table's token fields are cumulative across all turns, so we
    instead read the last assistant message's token data (which reflects the
    actual context window usage for that turn, like CC's usage field).
    """
    db = OPENCODE_DB_PATH
    if not db.exists():
        return None
    session_id = _find_opencode_session(pane_cwd, opencode_pid, session_id=session_id)
    if not session_id:
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            # Get model from session table
            row = conn.execute(
                "SELECT model FROM session WHERE id = ?",
                (session_id,),
            ).fetchone()
            # Get last assistant message with non-zero token data
            msg_row = conn.execute(
                "SELECT data FROM message "
                "WHERE session_id = ? AND data LIKE '%\"tokens\"%' "
                "AND json_extract(data, '$.tokens.input') > 0 "
                "ORDER BY time_created DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row or not msg_row:
        return None
    model_raw = row[0]
    model = "unknown"
    provider_id = ""
    model_config = None
    if model_raw:
        try:
            m = json.loads(model_raw) if isinstance(model_raw, str) else model_raw
            if isinstance(m, dict):
                model_config = m
                model = m.get("id") or "unknown"
                provider_id = m.get("providerID") or m.get("provider") or ""
            else:
                model = str(m)
        except Exception:
            model = str(model_raw)
    # Parse token data from last assistant message
    try:
        msg_data = json.loads(msg_row[0])
    except Exception:
        return None
    tokens_data = msg_data.get("tokens") or {}
    ti = tokens_data.get("input") or 0
    to = tokens_data.get("output") or 0
    tr = tokens_data.get("reasoning") or 0
    cache = tokens_data.get("cache") or {}
    tcr = cache.get("read") or 0
    tcw = cache.get("write") or 0
    window = _opencode_model_window(provider_id, model, model_config)
    tokens = tokens_data.get("total") or (ti + tcr + tcw)  # What the model actually saw
    return {
        "model": model,
        "provider": provider_id,
        "session_id": session_id,
        "tokens": tokens,
        "window": window,
        "pct": round(tokens / window * 100, 2) if window else 0,
        "breakdown": {
            "input": ti,
            "cache_read": tcr,
            "cache_creation": tcw,
            "output": to,
        },
    }


_WINDOW_CACHE: dict = {}
_MODEL_CMD_RE = re.compile(
    rb"<command-name>/model</command-name>.*?<command-args>([^<]*)</command-args>",
    re.DOTALL,
)


def _detect_session_window(jsonl_path: Path) -> Optional[int]:
    """Find the user's effective context window from /model commands.

    Scans the whole jsonl (cached by mtime) so we catch /model invocations
    from earlier in long sessions. Returns 1_000_000, 200_000, or None
    when the user never ran /model (caller falls back to other signals).
    """
    if not jsonl_path or not jsonl_path.exists():
        return None
    try:
        mtime = jsonl_path.stat().st_mtime_ns
    except OSError:
        return None
    key = str(jsonl_path)
    cached = _WINDOW_CACHE.get(key)
    if cached and cached["mtime"] == mtime:
        return cached["value"]
    try:
        with open(jsonl_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    matches = _MODEL_CMD_RE.findall(data)
    value = None
    # 从后往前找第一个"看起来像 model slug"的 arg。
    # jsonl 后续 turn 里如果用户/assistant 引用了笔记或文档,
    # 里面可能包含 `<command-name>/model</command-name>...<command-args>...</command-args>`
    # 这种 meta 字符串(比如笔记里的省略号占位),要跳过。
    for raw in reversed(matches):
        last = raw.decode("utf-8", errors="ignore").strip()
        ll = last.lower()
        if "[1m]" in ll:
            value = 1_000_000
            break
        if ll == "fable" or ll.startswith("claude-fable"):
            value = 1_000_000
            break
        if ll == "default":
            # ambiguous — fall back to other signals
            break
        if ll.startswith("claude-") or ll.startswith("claude_"):
            value = 200_000
            break
        # 其它(笔记里的占位 `...` / 截断 / 任意文本):跳过,继续往前找
    # Fallback: 没找到 /model 命令(常见于 resume 进来的 session,/model 在父 jsonl
    # 里)。整文件扫 `[1m]` / `[1M]` 标记 —— 对话里要引用模型 slug 一般也用这格式。
    if value is None:
        if b"[1m]" in data or b"[1M]" in data:
            value = 1_000_000
    _WINDOW_CACHE[key] = {"mtime": mtime, "value": value}
    return value


def _read_last_usage(jsonl_path: Path) -> Optional[dict]:
    """Tail jsonl and return the last assistant event's usage breakdown.

    tokens = input + cache_creation + cache_read (what the model actually saw).
    Window is detected from the user's last /model command (full-file scan,
    mtime-cached). Falls back to "[1M]" markers in the tail, then to a token-
    bucket sanity check so we never show > 100% on a wrong assumption.
    """
    if not jsonl_path or not jsonl_path.exists():
        return None
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            tail = min(size, 512 * 1024)
            f.seek(size - tail)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
    except Exception:
        return None
    last = None
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln or '"type":"assistant"' not in ln:
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant" or obj.get("isSidechain"):
            continue
        msg = obj.get("message") or {}
        if not (msg.get("usage") or {}):
            continue
        last = obj
        break
    if not last:
        return None
    msg = last.get("message") or {}
    usage = msg.get("usage") or {}
    breakdown = {
        "input": int(usage.get("input_tokens") or 0),
        "cache_creation": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
    }
    tokens = breakdown["input"] + breakdown["cache_creation"] + breakdown["cache_read"]
    window = _detect_session_window(jsonl_path)
    if window is None:
        # No /model command found anywhere. Look for [1M] markers in the tail
        # (covers resumed sessions that inherited 1M from a parent context).
        # Case-insensitive: claude-opus-4-7[1m] uses lowercase in the slug.
        tail_lower = data.lower()
        window = 1_000_000 if ("[1m]" in tail_lower or "(1m" in tail_lower) else 200_000
    # Sanity: if model already saw more than the inferred window, it must be on
    # a larger one. The only standard tier above 200k is 1M.
    if tokens > window:
        window = 1_000_000
    return {
        "model": msg.get("model") or "unknown",
        "session_id": last.get("sessionId"),
        "tokens": tokens,
        "window": window,
        "pct": round(tokens / window * 100, 2) if window else 0,
        "breakdown": breakdown,
        "ts": _parse_iso_ts(last.get("timestamp")),
    }


def _session_activity(name: str, info: dict) -> Tuple[str, Optional[float]]:
    """Returns (summary, last_message_at). For Claude Code sessions, both come
    from the conversation JSONL so the timestamp reflects real interaction —
    not the pane log's mtime, which CC's TUI bumps every second via spinner/
    status-footer redraws even when the user is idle.

    Falls back to the pane snapshot for shell sessions."""
    if info.get("kind") == "cc":
        jsonl = _claude_jsonl_for_pid(info.get("claude_pid"))
        if jsonl:
            text, ts = _last_message_from_jsonl(jsonl)
            if text:
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) > 140:
                    text = text[:140] + "…"
                return text, ts
    if info.get("kind") == "codex":
        jsonl = _find_codex_jsonl(info.get("codex_pid"), session_name=name)
        if jsonl:
            text, ts = _last_codex_message_from_jsonl(jsonl)
            if text:
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) > 140:
                    text = text[:140] + "…"
                return text, ts
    if info.get("kind") == "opencode":
        try:
            text, ts = _last_opencode_message(pane_cwd=info.get("cwd"),
                                               session_id=info.get("opencode_session_id"),
                                               opencode_pid=info.get("opencode_pid"))
            if text:
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) > 140:
                    text = text[:140] + "…"
                return text, ts
        except Exception:
            pass
    return _summarize_pane(name), None


def list_sessions() -> list[dict]:
    raw_sessions = _tmux_sessions_raw()
    # Guard: only archive missing sessions if tmux returned a non-empty list.
    # An empty list could mean tmux is restarting or had a transient error —
    # archiving everything in that case would be destructive.
    if raw_sessions:
        _auto_archive_missing_sessions({s["name"] for s in raw_sessions})
    out = []
    state_entries = {}
    for s in raw_sessions:
        info = _pane_info(s["name"]) or {}
        kind = info.get("kind", "shell")
        cwd = info.get("cwd")
        cwd_short = cwd
        if cwd and cwd.startswith(HOME + "/"):
            cwd_short = cwd[len(HOME)+1:]
        elif cwd == HOME:
            cwd_short = "~"
        log_p = log_path(s["name"])
        log_mtime = log_p.stat().st_mtime if log_p.exists() else 0
        summary, last_msg_at = _session_activity(s["name"], info)
        avatar_p = find_avatar_path(s["name"])
        row = {
            "name": s["name"],
            "display_name": get_display_name(s["name"]),
            "chat_name": get_chat_name(s["name"]),
            "has_avatar": avatar_p is not None,
            "avatar_mtime": avatar_p.stat().st_mtime if avatar_p is not None else 0,
            "created": s["created"],
            "attached": s["attached"],
            "windows": s["windows"],
            "cwd": cwd,
            "cwd_short": cwd_short,
            "pane_cmd": info.get("pane_cmd"),
            "kind": kind,
            "is_cc": kind == "cc",
            "claude_pid": info.get("claude_pid"),
            "codex_pid": info.get("codex_pid"),
            "opencode_pid": info.get("opencode_pid"),
            "opencode_session_id": info.get("opencode_session_id"),
            "log_mtime": log_mtime,
            "log_size": log_p.stat().st_size if log_p.exists() else 0,
            "last_line": summary,
            "last_message_at": last_msg_at,
        }
        out.append(row)
        state_entries[s["name"]] = _snapshot_live_session(s, info, row)
    _write_live_state(state_entries)
    return out


def capture_history(session: str, lines: int = 2000) -> str:
    """Get scrollback from tmux pane. Used for initial render before SSE catches up.

    Appends a cursor-positioning escape so xterm.js's cursor lands where tmux thinks
    it is — critical for TUIs like Claude Code that emit relative cursor moves
    (\\r\\n + \\x1b[NA) anchored at the current cursor row.
    """
    if not NAME_RE.match(session):
        return ""
    # -e includes ANSI escape sequences (colors); -p prints to stdout
    r = _run(["tmux", "capture-pane", "-ep", "-t", session, "-S", f"-{int(lines)}"], timeout=5)
    if r.returncode != 0:
        return ""
    # Convert bare LF to CRLF: tmux returns each row terminated by \n only, but
    # rows that fill the full pane width leave the cursor in pending-wrap state
    # — LF then moves cursor down 1 row WITHOUT resetting the column, so the
    # next row's content lands at col N instead of col 0. Adding the CR fixes
    # the column on every row. (This is why Claude Code panes — which have
    # full-width 51-char ─ separators — renders garbled, but codex panes
    # render fine.)
    body = r.stdout.rstrip("\n").replace("\n", "\r\n")
    # Clear xterm.js's grid + home cursor so the pane contents land at row 1,
    # then query tmux for the real cursor and position xterm.js's cursor to match.
    # This is critical for TUIs like Claude Code that emit relative cursor moves
    # (\\r\\n + \\x1b[NA) anchored at the current cursor row.
    prefix = "\x1b[2J\x1b[H"
    suffix = ""
    cp = _run(["tmux", "display-message", "-t", session, "-p", "#{cursor_x},#{cursor_y}"], timeout=2)
    if cp.returncode == 0:
        parts = cp.stdout.strip().split(",")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            cx = int(parts[0]) + 1  # tmux is 0-indexed, CSI H is 1-indexed
            cy = int(parts[1]) + 1
            suffix = f"\x1b[{cy};{cx}H"
    return prefix + body + suffix


def capture_history_snapshot(session: str, lines: int = 2000) -> dict:
    """Return an initial pane snapshot plus a log position for SSE catch-up.

    Measuring the log immediately after capture reduces the old request/response
    gap to a very small capture-to-stat window; all later bytes are replayed by
    `/stream?offset=...` instead of being silently skipped while the browser
    establishes EventSource.
    """
    # A screen snapshot must not tear down a live pipe: `/history` is also
    # used by auxiliary UI probes, and close/reopen races can strand the pane
    # with no SSE output until the next reconnect. Only repair missing pipes.
    ensure_pipe(session)
    history = capture_history(session, lines)
    try:
        stream_offset = log_path(session).stat().st_size
    except OSError:
        stream_offset = 0
    return {"history": history, "stream_offset": stream_offset}


_OAUTH_URL_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-._~:/?#[]@!$&'()*+,;=%"
)


def detect_login_state(session: str) -> dict:
    """Inspect the pane buffer for Claude Code's /login prompt and extract
    the OAuth URL. Uses tmux `-J` so wrapped lines are joined into one, then
    walks forward across any leftover hard newlines that CC's narrow box
    inserts inside the URL."""
    if not NAME_RE.match(session):
        return {"active": False}
    r = _run(["tmux", "capture-pane", "-J", "-p", "-t", session, "-S", "-300"], timeout=3)
    if r.returncode != 0:
        return {"active": False}
    raw = ANSI_RE.sub("", r.stdout)
    if "Paste code here" not in raw or "oauth" not in raw.lower():
        return {"active": False}
    # Find the OAuth URL — anchor on "https://" preceding "oauth/authorize"
    auth_idx = raw.find("oauth/authorize")
    if auth_idx == -1:
        return {"active": True, "url": None}
    scheme_idx = raw.rfind("https://", 0, auth_idx)
    if scheme_idx == -1:
        return {"active": True, "url": None}
    chars = []
    i = scheme_idx
    while i < len(raw):
        ch = raw[i]
        if ch in _OAUTH_URL_CHARS:
            chars.append(ch)
            i += 1
        elif ch in "\n\r":
            # Skip the newline + any leading whitespace and continue if the
            # next non-space char is still URL-safe.
            j = i + 1
            while j < len(raw) and raw[j] in " \t":
                j += 1
            if j < len(raw) and raw[j] in _OAUTH_URL_CHARS:
                i = j
                continue
            break
        else:
            break
    url = "".join(chars)
    if not url.startswith("http"):
        return {"active": True, "url": None}
    return {"active": True, "url": url}


def trim_log_if_needed(session: str) -> None:
    p = log_path(session)
    try:
        if p.stat().st_size > MAX_LOG_BYTES:
            # Keep last half
            with p.open("rb") as f:
                f.seek(-MAX_LOG_BYTES // 2, os.SEEK_END)
                tail = f.read()
            with p.open("wb") as f:
                f.write(tail)
    except (FileNotFoundError, OSError):
        pass


_LOG_TRIM_INTERVAL_SEC = 30 * 60  # every 30 min


def _log_trimmer_loop():
    """Background daemon: trim oversized session logs.

    The function `trim_log_if_needed` was defined but never invoked anywhere,
    so logs grew unbounded (a single session log reached 87 MB before this was
    wired).
    Race with concurrent O_APPEND writers is acceptable here — at worst a
    handful of bytes written during the microsecond truncate window are lost
    from cosmetic dashboard scrollback (real conversation lives in jsonl)."""
    while True:
        try:
            time.sleep(_LOG_TRIM_INTERVAL_SEC)
            if not LOG_DIR.exists():
                continue
            for log_file in LOG_DIR.glob("*.log"):
                trim_log_if_needed(log_file.stem)
        except Exception:
            # never let the daemon die; just skip this round
            pass


_log_trimmer_thread = threading.Thread(target=_log_trimmer_loop, daemon=True, name="log-trimmer")
_log_trimmer_thread.start()


async def tail_log_sse(session: str, request_is_connected, offset: Optional[int] = None) -> "asyncio.AsyncIterator[str]":
    """SSE generator: stream exact terminal bytes from an optional log offset."""
    if not NAME_RE.match(session):
        yield 'event: error\ndata: invalid session name\n\n'
        return
    ensure_pipe(session)
    p = log_path(session)
    p.touch(exist_ok=True)
    fh = p.open("rb")
    fh.seek(0, os.SEEK_END)
    end_offset = fh.tell()
    if offset is not None and 0 <= offset <= end_offset:
        fh.seek(offset, os.SEEK_SET)
    last_keepalive = time.time()
    try:
        while True:
            if not await request_is_connected():
                break
            chunk = fh.read(65536)
            if chunk:
                # Keep the PTY stream byte-exact. Decoding each read chunk as UTF-8
                # corrupts a Chinese/emoji character when its bytes cross a read
                # boundary; xterm can decode the original Uint8Array correctly.
                payload = {
                    "encoding": "base64",
                    "data": base64.b64encode(chunk).decode("ascii"),
                }
                yield "data: " + json.dumps(payload) + "\n\n"
                last_keepalive = time.time()
            else:
                # No new bytes: send keepalive every 15s to keep connection through proxies
                if time.time() - last_keepalive > 15:
                    yield ": keepalive\n\n"
                    last_keepalive = time.time()
                await asyncio.sleep(0.3)
    finally:
        fh.close()


def validate_new_session(name: str, cwd: str) -> Optional[str]:
    if not NAME_RE.match(name):
        return "name must be 1–32 chars: letters, digits, dash, underscore"
    if name in {s["name"] for s in _tmux_sessions_raw()}:
        return f"session {name!r} already exists"
    if not cwd or not os.path.isdir(cwd):
        return "cwd must be an existing directory"
    real = os.path.realpath(cwd)
    if not real.startswith(HOME):
        return f"cwd must be under {HOME}"
    return None


def create_session(name: str, cwd: str, session_type: str = "cc", cols: int = 80, rows: int = 24, resume_sid: Optional[str] = None, setting_sources: Optional[str] = None, with_telegram: bool = False) -> dict:
    cwd = os.path.expanduser(cwd or "")
    err = validate_new_session(name, cwd)
    if err:
        return {"ok": False, "error": err}
    if session_type not in ("cc", "shell", "codex", "opencode"):
        return {"ok": False, "error": "session_type must be 'cc', 'shell', 'codex', or 'opencode'"}
    cols = max(20, min(500, int(cols)))
    rows = max(10, min(200, int(rows)))
    cwd_real = os.path.realpath(cwd)
    if session_type == "shell":
        wrapped = f"cd {cwd_real} && exec bash -l"
    elif session_type == "codex":
        wrapped = f"cd {cwd_real} && while true; do codex; sleep 3; done"
    elif session_type == "opencode":
        wrapped = f"cd {cwd_real} && while true; do opencode; sleep 3; done"
    else:
        args = ["claude", "--dangerously-skip-permissions"]
        if with_telegram:
            # Wires the official Telegram channel plugin onto this session so
            # bot DMs route here. Only one CC pane at a time can own the bot
            # (single getUpdates consumer per token) — see transfer_telegram.
            args.extend(["--channels", "plugin:telegram@claude-plugins-official"])
        if setting_sources:
            args.extend(["--setting-sources", setting_sources])
        if resume_sid:
            args.extend(["--resume", resume_sid])
        cmd = " ".join(args)
        # Ensure bun-based tooling is on PATH for dashboard-created sessions.
        wrapped = f"export BUN_INSTALL=\"$HOME/.bun\"; export PATH=\"$BUN_INSTALL/bin:$PATH\"; cd {cwd_real} && while true; do {cmd}; sleep 3; done"
    r = _run(["tmux", "new-session", "-d", "-s", name, "-x", str(cols), "-y", str(rows), "bash", "-c", wrapped], timeout=10)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip() or "tmux failed"}
    ensure_pipe(name)
    return {"ok": True, "name": name, "cwd": cwd_real, "type": session_type, "cols": cols, "rows": rows}


# ── Telegram channel mounting ──
#
# The Telegram MCP plugin polls one bot token via long-poll getUpdates. Telegram
# allows exactly one consumer per token, so at most one CC session can "own" the
# bot at a time. The plugin guards this with a PID file (server.ts), but the
# dashboard layer also lets you *move* the bot to a different session at runtime
# — that's what `transfer_telegram` does: rebuild target with --channels, rebuild
# every other holder without, all under --resume so chats survive.

def _current_session_sid(session: str) -> Optional[str]:
    """Get the resume SID for a running CC session (its JSONL stem)."""
    info = _pane_info(session)
    if not info or info.get("kind") != "cc":
        return None
    jsonl = _claude_jsonl_for_pid(info.get("claude_pid"))
    return jsonl.stem if jsonl else None


def _has_telegram(session: str) -> bool:
    info = _pane_info(session)
    return bool(info and "plugin:telegram" in (info.get("claude_args") or ""))


def telegram_session_status(session: str, info: Optional[dict] = None) -> dict:
    """Return configured vs actually-running Telegram channel state.

    `--channels plugin:telegram...` only means Claude was started with the
    channel argument. The channel is really available only after the plugin's
    bun server is running under that tmux pane.
    """
    if not NAME_RE.match(session):
        return {"session": session, "configured": False, "running": False, "state": "none"}
    if info is None:
        info = _pane_info(session)
    configured = bool(info and info.get("kind") == "cc" and "plugin:telegram" in (info.get("claude_args") or ""))
    running = False
    if info and info.get("pane_pid"):
        for _pid, comm, args in _walk_descendants(info["pane_pid"]):
            if "claude-plugins-official/telegram" in args and ("bun" in comm or "bun" in args):
                running = True
                break
    state = "connected" if running else ("configured" if configured else "none")
    return {"session": session, "configured": configured, "running": running, "state": state}


def telegram_status() -> dict:
    """Survey all sessions; return who's currently holding the bot vs configured."""
    sessions_status = []
    holder = None
    configured_holder = None
    for s in _tmux_sessions_raw():
        status = telegram_session_status(s["name"])
        if status["state"] != "none":
            sessions_status.append(status)
        if status["running"] and holder is None:
            holder = s["name"]
        if status["configured"] and configured_holder is None:
            configured_holder = s["name"]
    return {"holder": holder, "configured_holder": configured_holder, "sessions": sessions_status}


def telegram_holder() -> Optional[str]:
    """Session whose Telegram plugin process is actually running."""
    return telegram_status().get("holder")


def telegram_configured_holder() -> Optional[str]:
    """Session started with the Telegram channel argument."""
    return telegram_status().get("configured_holder")


def _isolation_setting_sources(cwd: str) -> Optional[str]:
    """Return "user" if the project at `cwd` enables the Telegram plugin.

    A session that should NOT own Telegram is rebuilt without --channels, but
    that alone is not enough: if its cwd's project settings enable the plugin,
    being in that cwd auto-loads it as a plain MCP and the session re-grabs the
    single-instance bot — then can't deliver inbound, having no channel wiring
    (the recurring "TG 又断" split-brain). Pinning it to user-level settings
    makes it ignore the project's enabledPlugins so it never touches the bot.
    """
    try:
        sp = os.path.join(os.path.realpath(cwd), ".claude", "settings.json")
        with open(sp, "r", encoding="utf-8") as f:
            plugins = (json.load(f) or {}).get("enabledPlugins") or {}
        if any("telegram" in str(k) and v for k, v in plugins.items()):
            return "user"
    except (OSError, json.JSONDecodeError, AttributeError, ValueError):
        pass
    return None


def _telegram_session_snapshot(session: str) -> dict:
    """Capture everything we need to rebuild `session` with a different TG state."""
    info = _pane_info(session) or {}
    return {
        "name": session,
        "cwd": info.get("cwd") or HOME,
        "sid": _current_session_sid(session),
        "display": get_display_name(session),
        "chat": get_chat_name(session),
    }


def _rebuild_session(snap: dict, with_telegram: bool, setting_sources: Optional[str] = None) -> dict:
    """Kill `snap['name']` and re-create it under --resume with the requested
    Telegram state. Restores display/chat names afterward."""
    name = snap["name"]
    kill_session(name, allow_primary=True)
    time.sleep(0.5)
    r = create_session(
        name,
        snap["cwd"],
        session_type="cc",
        resume_sid=snap.get("sid"),
        with_telegram=with_telegram,
        setting_sources=setting_sources,
    )
    if r.get("ok"):
        if snap.get("display"):
            set_display_name(name, snap["display"])
        if snap.get("chat"):
            set_chat_name(name, snap["chat"])
    return r


def transfer_telegram(target: str) -> dict:
    """Move the Telegram channel onto `target`, rebuilding every session that
    currently holds the bot so exactly one owns it. All rebuilds use --resume so
    conversations are preserved.

    "Holder" is plural by design: the session started with --channels
    (configured_holder) and the session whose bun poller is actually running
    (holder) can differ — a non-channel session in the bot's cwd grabs it via
    project enabledPlugins. We strip telegram from ALL of them except the
    target, and isolate each with --setting-sources user where the project
    enables the plugin so it can't re-grab the single-instance bot."""
    if not NAME_RE.match(target):
        return {"ok": False, "error": "invalid target name"}

    target_info = _pane_info(target)
    if not target_info or target_info.get("kind") != "cc":
        return {"ok": False, "error": f"{target} is not a CC session"}

    status = telegram_status()
    running_holder = status.get("holder")
    configured_holder = status.get("configured_holder")
    # dict.fromkeys preserves order and dedups; drop the target itself.
    sources = [s for s in dict.fromkeys([configured_holder, running_holder]) if s and s != target]

    if not sources and running_holder == target and configured_holder == target:
        return {"ok": True, "already": True, "holder": target}

    # Snapshot everyone BEFORE any kill, so resume sids stay readable.
    target_snap = _telegram_session_snapshot(target)
    source_snaps = [_telegram_session_snapshot(s) for s in sources]

    # 1. Rebuild target WITH telegram so it claims the bot.
    r = _rebuild_session(target_snap, with_telegram=True)
    if not r.get("ok"):
        return {"ok": False, "error": f"recreate target failed: {r.get('error')}",
                "sources": sources, "target": target}

    # 2. Strip telegram from every other holder, isolating so it can't re-grab.
    stripped = []
    for snap in source_snaps:
        r2 = _rebuild_session(snap, with_telegram=False,
                              setting_sources=_isolation_setting_sources(snap["cwd"]))
        if not r2.get("ok"):
            return {"ok": False, "error": f"recreate source {snap['name']} failed: {r2.get('error')}",
                    "sources": sources, "target": target, "target_ok": True, "stripped": stripped}
        stripped.append(snap["name"])

    return {"ok": True, "sources": stripped, "target": target, "target_sid": target_snap["sid"]}


def resize_session(session: str, cols: int, rows: int) -> dict:
    if not NAME_RE.match(session):
        return {"ok": False, "error": "invalid name"}
    # Floor raised from 10x5 to 30x10: xterm.js on iOS can briefly measure tiny
    # dims (visualViewport quirks, hidden DOM, orientation flip). With tmux's
    # `window-size latest`, those tiny dims got pinned and stranded the pane
    # with no attached client to撑 it back. Reject豆腐条 sizes server-side.
    if not (30 <= cols <= 500 and 10 <= rows <= 200):
        return {"ok": False, "error": "size out of range"}
    r = _run(["tmux", "resize-window", "-t", session, "-x", str(cols), "-y", str(rows)], timeout=3)
    if r.returncode != 0:
        # fallback: some tmux versions need resize-pane
        r = _run(["tmux", "resize-pane", "-t", session, "-x", str(cols), "-y", str(rows)], timeout=3)
        if r.returncode != 0:
            return {"ok": False, "error": r.stderr.strip() or "resize failed"}
    return {"ok": True, "cols": cols, "rows": rows}


def send_input(session: str, data: str) -> dict:
    """Send keys to a tmux pane.

    Text is pasted through a tmux buffer, then trailing \r is sent as a
    translated Enter key. Buffer paste is more reliable than thousands of
    literal key events for Codex/Claude TUIs.
    """
    if not NAME_RE.match(session):
        return {"ok": False, "error": "invalid name"}
    if not data:
        return {"ok": True}
    # Claude Code's expanded transcript is read-only. Text sent while it is open
    # is accepted by tmux but never reaches the prompt, which looks like a
    # successful Chat send with no delivered message.
    if data != "\x0f":
        pane = _run(["tmux", "capture-pane", "-p", "-t", session, "-S", "-8"], timeout=3)
        if pane.returncode == 0 and "Showing detailed transcript" in pane.stdout:
            toggle = _run(["tmux", "send-keys", "-t", session, "C-o"], timeout=3)
            if toggle.returncode != 0:
                return {"ok": False, "error": toggle.stderr.strip() or "cannot close transcript view"}
            time.sleep(0.1)
    pane_info = _pane_info(session) or {}
    is_claude_code = pane_info.get("kind") == "cc"
    trailing_enter = data.endswith("\r")
    text_part = data[:-1] if trailing_enter else data
    try:
        if text_part:
            set_buffer = subprocess.run(
                ["tmux", "load-buffer", "-w", "-"],
                input=text_part.encode("utf-8"),
                capture_output=True, timeout=3,
            )
            if set_buffer.returncode == 0:
                r = subprocess.run(
                    ["tmux", "paste-buffer", "-d", "-t", session],
                    capture_output=True, timeout=3,
                )
                if r.returncode != 0:
                    return {"ok": False, "error": r.stderr.decode("utf-8", errors="replace").strip()}
            else:
                r = subprocess.run(
                    ["tmux", "send-keys", "-t", session, "-l", text_part],
                    capture_output=True, timeout=3,
                )
                if r.returncode != 0:
                    return {"ok": False, "error": r.stderr.decode("utf-8", errors="replace").strip()}
        if trailing_enter:
            if text_part:
                paste_delay = 0.12 + min(0.5, len(text_part) / 20000)
                paste_delay += min(0.2, text_part.count("\n") * 0.005)
                if is_claude_code:
                    # Claude Code can take a moment to turn bracketed paste into
                    # its "[Pasted text #...]" prompt placeholder. Sending Enter
                    # too early leaves the whole message sitting in the input bar
                    # until a human presses Enter later.
                    paste_delay += 0.45
                time.sleep(paste_delay)
            r = subprocess.run(
                ["tmux", "send-keys", "-t", session, "Enter"],
                capture_output=True, timeout=3,
            )
            if r.returncode != 0:
                return {"ok": False, "error": r.stderr.decode("utf-8", errors="replace").strip()}
            if is_claude_code and text_part:
                time.sleep(0.35)
                pane = _run(["tmux", "capture-pane", "-p", "-t", session, "-S", "-12"], timeout=3)
                if pane.returncode == 0:
                    prompt_tail = _last_claude_prompt_tail(ANSI_RE.sub("", pane.stdout or "")) or ""
                    if "[Pasted text #" in prompt_tail:
                        r = subprocess.run(
                            ["tmux", "send-keys", "-t", session, "Enter"],
                            capture_output=True, timeout=3,
                        )
                        if r.returncode != 0:
                            return {"ok": False, "error": r.stderr.decode("utf-8", errors="replace").strip()}
        return {"ok": True}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "tmux send-keys timeout"}


def kill_session(name: str, allow_primary: bool = False) -> dict:
    if not NAME_RE.match(name):
        return {"ok": False, "error": "invalid name"}
    r = _run(["tmux", "kill-session", "-t", name], timeout=5)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip()}
    # Clean up the log file + all per-session sidecars
    try:
        log_path(name).unlink(missing_ok=True)
    except Exception:
        pass
    try:
        title_path(name).unlink(missing_ok=True)
    except Exception:
        pass
    try:
        chat_name_path(name).unlink(missing_ok=True)
    except Exception:
        pass
    clear_avatar(name)
    # User-initiated deletion should not be treated as an unexpected tmux loss.
    _drop_live_state(name)
    return {"ok": True}


def rename_session(old: str, new: str) -> dict:
    if not NAME_RE.match(old):
        return {"ok": False, "error": "invalid old name"}
    if not NAME_RE.match(new):
        return {"ok": False, "error": "new name must be 1–32 chars: letters, digits, dash, underscore"}
    if old == new:
        return {"ok": True, "name": new}
    existing = {s["name"] for s in _tmux_sessions_raw()}
    if old not in existing:
        return {"ok": False, "error": f"session {old!r} not found"}
    if new in existing:
        return {"ok": False, "error": f"session {new!r} already exists"}
    r = _run(["tmux", "rename-session", "-t", old, new], timeout=3)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip() or "tmux rename failed"}
    # Move the rolling log file so SSE tail continues from the new name.
    old_log = log_path(old)
    new_log = log_path(new)
    if old_log.exists():
        try:
            old_log.replace(new_log)
        except Exception as e:
            return {"ok": False, "error": f"renamed in tmux but log move failed: {e}"}
    # Rename is the rare case where the output target must change; replace it
    # in one tmux command rather than close/reopen with an observable gap.
    _run(["tmux", "pipe-pane", "-t", new, f"cat >> {log_path(new)}"])
    # Carry the display title sidecar across the rename so the UI label sticks.
    old_title = title_path(old)
    if old_title.exists():
        try:
            old_title.replace(title_path(new))
        except Exception:
            pass
    # Carry chat name + avatar so Chats view metadata sticks too.
    old_chat = chat_name_path(old)
    if old_chat.exists():
        try:
            old_chat.replace(chat_name_path(new))
        except Exception:
            pass
    old_av = find_avatar_path(old)
    if old_av is not None:
        try:
            old_av.replace(LOG_DIR / f"{new}.avatar{old_av.suffix}")
        except Exception:
            pass
    return {"ok": True, "name": new}


def _read_live_state() -> dict:
    try:
        data = json.loads(LIVE_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_live_state(state: dict) -> None:
    try:
        tmp = LIVE_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(LIVE_STATE_PATH)
    except Exception:
        pass


def _drop_live_state(name: str) -> None:
    state = _read_live_state()
    if name in state:
        state.pop(name, None)
        _write_live_state(state)


def _snapshot_live_session(tmux_row: dict, info: dict, row: dict) -> dict:
    jsonl_path = None
    session_id = None
    if info.get("kind") == "cc":
        jp = _claude_jsonl_for_pid(info.get("claude_pid"))
        if jp is not None:
            jsonl_path = str(jp)
            session_id = jp.stem
    return {
        "name": row.get("name"),
        "display_name": row.get("display_name"),
        "chat_name": row.get("chat_name"),
        "kind": row.get("kind") or "shell",
        "cwd": row.get("cwd"),
        "created": tmux_row.get("created"),
        "jsonl_path": jsonl_path,
        "session_id": session_id,
        "log_path": str(log_path(row.get("name", ""))),
        "last_seen_at": int(time.time()),
    }


def _jsonl_message_count(jsonl_path: Optional[str]) -> int:
    if not jsonl_path or not Path(jsonl_path).exists():
        return 0
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _archive_duplicate_exists(snapshot: dict) -> bool:
    jp = snapshot.get("jsonl_path")
    if not jp:
        return False
    d = _archive_dir()
    if not d.exists():
        return False
    for meta_path in d.glob("*.json"):
        meta = _read_archive_meta(meta_path)
        if meta and meta.get("jsonl_path") == jp:
            return True
    return False


def _archive_snapshot(snapshot: dict, reason: str = "unexpected_close") -> Optional[dict]:
    name = snapshot.get("name") or "session"
    if not NAME_RE.match(name):
        return None
    if _archive_duplicate_exists(snapshot):
        return None
    archive_dir = _archive_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    archive_id = f"{name}-auto-{ts}"
    jsonl_path = snapshot.get("jsonl_path")
    preview = ""
    if jsonl_path and Path(jsonl_path).exists():
        preview = _first_user_text(Path(jsonl_path)) or ""
    display_name = snapshot.get("display_name")
    chat_name = snapshot.get("chat_name")
    meta = {
        "archive_id": archive_id,
        "name": name,
        "display_name": display_name,
        "chat_name": chat_name,
        "kind": snapshot.get("kind") or "shell",
        "cwd": snapshot.get("cwd"),
        "session_id": snapshot.get("session_id"),
        "jsonl_path": jsonl_path,
        "archived_at": ts,
        "preview": preview,
        "msg_count": _jsonl_message_count(jsonl_path),
        "auto_archived": True,
        "interrupted": True,
        "reason": reason,
        "last_seen_at": snapshot.get("last_seen_at"),
    }
    log_src = Path(snapshot.get("log_path") or str(log_path(name)))
    if log_src.exists():
        log_dst = archive_dir / f"{archive_id}.log"
        try:
            log_src.replace(log_dst)
            meta["log_path"] = str(log_dst)
        except Exception:
            pass
    try:
        (archive_dir / f"{archive_id}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return None
    try:
        title_path(name).unlink(missing_ok=True)
        chat_name_path(name).unlink(missing_ok=True)
    except Exception:
        pass
    clear_avatar(name)
    return meta


def _auto_archive_missing_sessions(current_names: set[str]) -> None:
    state = _read_live_state()
    if not state:
        return
    changed = False
    for name, snapshot in list(state.items()):
        if name in current_names:
            continue
        _archive_snapshot(snapshot, reason="missing_tmux_session")
        state.pop(name, None)
        changed = True
    if changed:
        _write_live_state(state)


def archive_session(name: str) -> dict:
    """Kill the tmux session but preserve its log file under LOG_DIR/archived/."""
    if not NAME_RE.match(name):
        return {"ok": False, "error": "invalid name"}
    existing = {s["name"] for s in _tmux_sessions_raw()}
    if name not in existing:
        return {"ok": False, "error": f"session {name!r} not found"}
    archive_dir = LOG_DIR / "archived"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    # Snapshot the JSONL pointer + identity BEFORE killing tmux. Once the pane
    # is gone, _pane_info can't find the claude PID, and ~/.claude/sessions/
    # entries get GC'd when the process exits, so this is our only chance.
    info = _pane_info(name) or {}
    jsonl_path: Optional[str] = None
    session_id: Optional[str] = None
    if info.get("kind") == "cc":
        jp = _claude_jsonl_for_pid(info.get("claude_pid"))
        if jp is not None:
            jsonl_path = str(jp)
            session_id = jp.stem
    elif info.get("kind") == "codex":
        jp = _find_codex_jsonl(info.get("codex_pid"), session_name=name)
        if jp is not None:
            jsonl_path = str(jp)
            session_id = _codex_session_id(jp)
    display_name = get_display_name(name)
    chat_name = get_chat_name(name)
    # Compute the preview NOW (while archiving) so the list endpoint doesn't
    # have to re-scan every JSONL on every page load. The list endpoint can be
    # 50ms or 5s depending on how many large conversations are archived.
    preview = ""
    if jsonl_path and Path(jsonl_path).exists():
        if info.get("kind") == "codex":
            preview = _first_codex_user_text(Path(jsonl_path)) or ""
        else:
            preview = _first_user_text(Path(jsonl_path)) or ""
    # Also store a cheap message count so the UI can show "N messages" without
    # re-reading the file.
    msg_count = 0
    if jsonl_path and Path(jsonl_path).exists():
        try:
            with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
                msg_count = sum(1 for _ in f)
        except Exception:
            pass
    meta = {
        "archive_id": f"{name}-{ts}",
        "name": name,
        "display_name": display_name,
        "chat_name": chat_name,
        "kind": info.get("kind") or "shell",
        "cwd": info.get("cwd"),
        "session_id": session_id,
        "jsonl_path": jsonl_path,
        "archived_at": ts,
        "preview": preview,
        "msg_count": msg_count,
    }
    src = log_path(name)
    dst = archive_dir / f"{name}-{ts}.log"
    archived_path = None
    if src.exists():
        try:
            src.replace(dst)
            archived_path = str(dst)
        except Exception as e:
            return {"ok": False, "error": f"failed to preserve log: {e}"}
    try:
        meta_dst = archive_dir / f"{name}-{ts}.json"
        meta_dst.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"failed to write archive meta: {e}"}
    r = _run(["tmux", "kill-session", "-t", name], timeout=5)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip() or "tmux kill failed"}
    try:
        title_path(name).unlink(missing_ok=True)
    except Exception:
        pass
    try:
        chat_name_path(name).unlink(missing_ok=True)
    except Exception:
        pass
    clear_avatar(name)
    _drop_live_state(name)
    return {"ok": True, "name": name, "log": archived_path, "archive_id": meta["archive_id"]}


def _archive_dir() -> Path:
    return LOG_DIR / "archived"


def _read_archive_meta(meta_path: Path) -> Optional[dict]:
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _first_user_text(jsonl: Path) -> Optional[str]:
    """Return the first displayable user message text in a JSONL, capped at
    120 chars. Used to populate the archive preview without forcing the list
    endpoint to re-read the whole file. Skips synthetic <command-name> turns
    and tool_result entries which aren't real user prose."""
    try:
        with open(jsonl, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                content = (obj.get("message") or {}).get("content")
                text = None
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            text = b.get("text")
                            break
                if text and not text.startswith("<"):
                    return text.strip().splitlines()[0][:120]
    except Exception:
        return None
    return None


def _first_codex_user_text(jsonl: Path) -> Optional[str]:
    """Return the first visible user text from a Codex rollout JSONL."""
    try:
        with open(jsonl, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "response_item":
                    continue
                payload = obj.get("payload") or {}
                if payload.get("type") != "message" or payload.get("role") != "user":
                    continue
                texts = []
                for block in payload.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "input_text":
                        continue
                    text = (block.get("text") or "").strip()
                    if not text or text.startswith("<environment_context>") or text.startswith("<permissions"):
                        continue
                    text = re.sub(r"<chat-input\b[^>]*/>\s*", "", text, flags=re.IGNORECASE).strip()
                    if text:
                        texts.append(text)
                if texts:
                    return " ".join(texts).strip().splitlines()[0][:120]
    except Exception:
        return None
    return None


def list_archived_sessions() -> list[dict]:
    """List every archive metadata entry, newest first. Reads cached preview
    + msg_count from disk — these are pre-computed at archive time. For old
    archives written before caching was added, we fall back to scanning the
    JSONL lazily and persist the result so the next call is fast."""
    d = _archive_dir()
    if not d.exists():
        return []
    rows: list[dict] = []
    for p in d.glob("*.json"):
        meta = _read_archive_meta(p)
        if meta is None:
            continue
        live_chat_name = get_chat_name(meta.get("name") or "")
        if live_chat_name and meta.get("chat_name") != live_chat_name:
            meta["chat_name"] = live_chat_name
            try:
                p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        # Lazy back-fill for archives created before preview caching landed.
        jp = meta.get("jsonl_path")
        if "preview" not in meta and jp and Path(jp).exists():
            if meta.get("kind") == "codex":
                meta["preview"] = _first_codex_user_text(Path(jp)) or ""
            else:
                meta["preview"] = _first_user_text(Path(jp)) or ""
            try:
                p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        rows.append(meta)
    rows.sort(key=lambda r: r.get("archived_at") or 0, reverse=True)
    return rows


def read_archived_messages(archive_id: str, limit: int = 200, focus_uuid: Optional[str] = None) -> list[dict]:
    """Return messages for an archived session by id. Falls back to empty when
    the JSONL is missing (e.g. archive created from a non-cc session)."""
    if not NAME_RE.match(archive_id.split("-")[0] if "-" in archive_id else archive_id):
        # archive_id is "{name}-{ts}" — only validate the name half loosely
        pass
    meta_path = _archive_dir() / f"{archive_id}.json"
    meta = _read_archive_meta(meta_path)
    if meta is None:
        return []
    jp = meta.get("jsonl_path")
    if not jp:
        log_file = Path(meta.get("log_path") or "")
        if log_file.exists():
            return _terminal_log_messages(log_file, meta.get("name") or archive_id, limit, focus_uuid=focus_uuid)
        return []
    p = Path(jp)
    if not p.exists():
        return []
    if meta.get("kind") == "codex":
        try:
            import message_store as _ms
            return _ms.read_codex_chat_messages(p, limit, focus_uuid=focus_uuid)
        except Exception:
            return []
    return read_chat_messages_from_jsonl(p, meta.get("name") or archive_id, limit, focus_uuid=focus_uuid)


def rename_archived(archive_id: str, display_name: Optional[str]) -> dict:
    """Update the visible name stored in archive metadata."""
    meta_path = _archive_dir() / f"{archive_id}.json"
    meta = _read_archive_meta(meta_path)
    if meta is None:
        return {"ok": False, "error": "not found"}
    new_name = (display_name or "").strip() or None
    meta["display_name"] = new_name
    meta["chat_name"] = new_name
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"write failed: {e}"}
    return {"ok": True, "archive_id": archive_id, "display_name": new_name, "chat_name": new_name}


def delete_archived(archive_id: str) -> dict:
    """Remove the archive metadata JSON + the preserved bash log. Leaves the
    Claude conversation JSONL alone — that's owned by ~/.claude, not us."""
    d = _archive_dir()
    meta_path = d / f"{archive_id}.json"
    log_path_ = d / f"{archive_id}.log"
    if not meta_path.exists() and not log_path_.exists():
        return {"ok": False, "error": "not found"}
    for p in (meta_path, log_path_):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    return {"ok": True, "archive_id": archive_id}


def search_archived(query: str, max_hits: int = 50) -> list[dict]:
    """Plain substring search across every archive's JSONL. Returns matching
    archives with a snippet of the matching message."""
    q = query.strip()
    if not q:
        return []
    q_lower = q.lower()
    hits: list[dict] = []
    for meta in list_archived_sessions():
        jp = meta.get("jsonl_path")
        if not jp or not Path(jp).exists():
            continue
        snippet = None
        try:
            with open(jp, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    if q_lower not in ln.lower():
                        continue
                    try:
                        obj = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    text = None
                    if meta.get("kind") == "codex" and obj.get("type") == "response_item":
                        payload = obj.get("payload") or {}
                        if payload.get("type") == "message":
                            want = "input_text" if payload.get("role") == "user" else "output_text"
                            for b in payload.get("content") or []:
                                if isinstance(b, dict) and b.get("type") == want and q_lower in (b.get("text") or "").lower():
                                    text = b.get("text")
                                    break
                    else:
                        content = (obj.get("message") or {}).get("content")
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            for b in content:
                                if isinstance(b, dict) and b.get("type") == "text":
                                    if q_lower in (b.get("text") or "").lower():
                                        text = b.get("text")
                                        break
                    if not text:
                        continue
                    idx = text.lower().find(q_lower)
                    start = max(0, idx - 40)
                    end = min(len(text), idx + len(q) + 80)
                    snippet = ("…" if start > 0 else "") + text[start:end].replace("\n", " ") + ("…" if end < len(text) else "")
                    break
        except Exception:
            continue
        if snippet:
            hits.append({**meta, "snippet": snippet})
            if len(hits) >= max_hits:
                break
    return hits
