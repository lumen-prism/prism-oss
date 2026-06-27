"""
Unified message store — 所有来源的聊天消息统一存储和查询。

支持的来源：
- cc: Claude Code session JSONL
- codex: OpenAI Codex session JSONL
- api: (未来) 直接API对话
"""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

PRISM_DATA_DIR = Path(os.path.expanduser(os.environ.get("PRISM_DATA_DIR", "~/.local/share/prism")))
DB_PATH = str(PRISM_DATA_DIR / "messages.db")

CC_PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))
CC_RECORDS_JSON_DIR = PRISM_DATA_DIR / "chat_records" / "cc" / "json"
# Configurable inbox dir for external-channel image attachments (no personal
# path hardcoded). Defaults to a neutral, almost-never-present location so the
# attachment-rendering regexes effectively no-op for OSS deployments.
_INBOX_DIR = Path(os.path.expanduser(os.environ.get("PRISM_INBOX_DIR", str(PRISM_DATA_DIR / "inbox"))))

def _cc_session_dirs():
    if not CC_PROJECTS_DIR.exists():
        return []
    return sorted(d for d in CC_PROJECTS_DIR.iterdir() if d.is_dir())

def _iter_cc_record_files():
    if not CC_RECORDS_JSON_DIR.exists():
        return []
    return sorted(CC_RECORDS_JSON_DIR.glob("*.json"))

def _load_cc_record(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None

CODEX_SESSIONS_DIR = Path(os.path.expanduser("~/.codex/sessions"))

_CHANNEL_RE = re.compile(r"<channel[^>]*>\n?(.*?)\n?</channel>", re.DOTALL)
_CHANNEL_ATTRS_RE = re.compile(r'(\w+)="([^"]*)"')

# Telegram MCP plugin — tools the assistant calls to reply / react via the bot.
# Reply text becomes a normal assistant text message (with a delivery marker so
# the UI can hint where it landed); react adds an emoji reaction to a prior
# inbound msg without producing its own bubble.
TG_REPLY_TOOLS = {
    "mcp__plugin_telegram_telegram__reply",
    "mcp__plugin_telegram_telegram__edit_message",
}
TG_REACT_TOOL = "mcp__plugin_telegram_telegram__react"
_CHAT_INPUT_RE = re.compile(r'<chat-input\b[^>]*>\s*(.*?)\s*</chat-input>', re.DOTALL | re.IGNORECASE)
_CHAT_INPUT_PREFIX_RE = re.compile(r'<chat-input\b[^>]*/>\s*', re.IGNORECASE)
_CHAT_INPUT_SENT_AT_RE = re.compile(r'<chat-input\b[^>]*\bsent_at="([^"]+)"[^>]*/>', re.IGNORECASE)
_OAI_MEM_CITATION_SUFFIX_RE = re.compile(
    r'\n*<oai-mem-citation>\s*.*?</oai-mem-citation>\s*$',
    re.DOTALL | re.IGNORECASE,
)

# ── Database ──

def _init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            name TEXT,
            auto_name TEXT,
            created_at TEXT,
            updated_at TEXT,
            metadata TEXT DEFAULT '{}',
            hidden INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            sender TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            ts TEXT NOT NULL,
            source TEXT NOT NULL,
            message_type TEXT DEFAULT 'text',
            status TEXT DEFAULT 'delivered',
            metadata TEXT DEFAULT '{}',
            source_uuid TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
        CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source);
        CREATE INDEX IF NOT EXISTS idx_messages_session_uuid ON messages(session_id, source_uuid);

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            content=messages,
            content_rowid=id
        );

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;
    """)
    # Schema migrations for new columns
    for col, tbl, coldef in [
        ("reply_to_id", "messages", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {coldef}")
        except sqlite3.OperationalError:
            pass


@contextmanager
def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.create_function("has_link", 1, lambda content: 1 if _URL_RE.search(content or "") else 0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_db(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Helpers ──

def _strip_channel(text):
    if not isinstance(text, str):
        return ""
    m = _CHANNEL_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def _strip_chat_input(text):
    if not isinstance(text, str):
        return ""
    text = _CHAT_INPUT_RE.sub(lambda match: match.group(1).strip(), text).strip()
    return _CHAT_INPUT_PREFIX_RE.sub("", text).strip()


def _chat_input_sent_at(text, fallback):
    if isinstance(text, str):
        match = _CHAT_INPUT_SENT_AT_RE.search(text)
        if match:
            return match.group(1)
    return fallback


def _strip_oai_mem_citation(text):
    """Hide Codex response provenance metadata from user-visible chat text."""
    if not isinstance(text, str):
        return ""
    return _OAI_MEM_CITATION_SUFFIX_RE.sub("", text).rstrip()


def _channel_attrs(text):
    if not isinstance(text, str):
        return {}
    m = re.search(r"<channel([^>]*)>", text)
    if not m:
        return {}
    return dict(_CHANNEL_ATTRS_RE.findall(m.group(1)))


def _iter_jsonl(path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return


# ── CC Adapter ──

def _parse_cc_session(jsonl_path):
    """Parse a Claude Code CC conversation JSONL into unified messages."""
    messages = []
    current_assistant = None
    started = False
    session_first_ts = None
    session_last_ts = None
    # Telegram-backed CC threads can re-include the same inbound msg across
    # multiple <channel> blocks; dedup by tg_message_id so the UI doesn't double up.
    seen_tg_message_ids = set()

    def flush_assistant():
        nonlocal current_assistant
        if current_assistant is None:
            return
        text = "\n\n".join(current_assistant["texts"]).strip()
        if text:
            meta = {}
            if current_assistant.get("files"):
                meta["files"] = current_assistant["files"]
            messages.append({
                "role": "assistant", "sender": "assistant", "content": text,
                "ts": current_assistant["ts"] or "", "message_type": "text",
                "source_uuid": current_assistant["uuid"], "metadata": meta,
            })
        for reaction in current_assistant.get("reactions", []):
            messages.append({
                "role": "assistant", "sender": "assistant", "content": reaction["emoji"],
                "ts": current_assistant["ts"] or "", "message_type": "reaction",
                "source_uuid": None,
                "metadata": {"target_msg_id": reaction.get("target_msg_id")},
            })
        current_assistant = None

    for obj in _iter_jsonl(jsonl_path):
        if obj.get("isSidechain"):
            continue
        msg_type = obj.get("type")
        ts = obj.get("timestamp") or ""
        uuid = obj.get("uuid")
        content = (obj.get("message") or {}).get("content")
        if msg_type == "attachment":
            attachment = obj.get("attachment") or {}
            queued_prompt = attachment.get("prompt") if attachment.get("type") == "queued_command" else None
            if isinstance(queued_prompt, str) and "<channel" in queued_prompt:
                msg_type = "user"
                content = queued_prompt

        if msg_type == "user":
            if obj.get("isCompactSummary"):
                continue
            if isinstance(content, list):
                kinds = {b.get("type") for b in content if isinstance(b, dict)}
                if "tool_result" in kinds:
                    continue
                text = "\n\n".join(
                    (b.get("text") or "").strip() for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
                )
            elif isinstance(content, str):
                text = _strip_channel(content) if "<channel" in content else _strip_chat_input(content)
            else:
                text = ""
            if not text or text.startswith(("<command-", "<bash-", "<local-command-", "<environment_context>", "<permissions", "[Request interrupted")):
                continue
            if isinstance(content, str):
                ts = _chat_input_sent_at(content, ts)
            attrs = _channel_attrs(content) if isinstance(content, str) else {}
            tg_message_id = attrs.get("message_id") if attrs else None
            if tg_message_id and tg_message_id in seen_tg_message_ids:
                continue
            if tg_message_id:
                seen_tg_message_ids.add(tg_message_id)
            started = True
            flush_assistant()
            if ts:
                session_first_ts = session_first_ts or ts
                session_last_ts = ts
            image_path = attrs.get("image_path") if attrs else None
            stored_text = text + (("\n@" + image_path) if image_path else "")
            user_meta = {}
            if attrs:
                if attrs.get("user"): user_meta["tg_user"] = attrs.get("user")
                if attrs.get("chat_id"): user_meta["tg_chat_id"] = attrs.get("chat_id")
                if tg_message_id: user_meta["tg_message_id"] = tg_message_id
            if image_path:
                user_meta["image_path"] = image_path
            messages.append({
                "role": "user", "sender": "user", "content": stored_text, "ts": ts,
                "message_type": "text", "source_uuid": uuid,
                "metadata": user_meta,
            })
            continue

        if msg_type != "assistant" or not started or not isinstance(content, list):
            continue
        if current_assistant is None:
            current_assistant = {"ts": ts, "uuid": uuid, "texts": [], "reactions": []}
        elif ts:
            current_assistant["ts"] = ts
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = (block.get("text") or "").strip()
                if text:
                    current_assistant["texts"].append(text)
                    current_assistant["uuid"] = uuid
                    current_assistant["ts"] = ts or current_assistant["ts"]
                    session_last_ts = ts or session_last_ts
            elif block_type == "tool_use":
                name = block.get("name", "") or ""
                inp = block.get("input") or {}
                if name in TG_REPLY_TOOLS:
                    # Telegram reply/edit_message — surface the text the assistant
                    # actually sent to TG as its own bubble (otherwise users only
                    # see tool_use noise and no outgoing message).
                    reply_text = (inp.get("text") or "").strip()
                    if reply_text:
                        flush_assistant()
                        messages.append({
                            "role": "assistant", "sender": "assistant", "content": reply_text,
                            "ts": ts or "", "message_type": "text",
                            "source_uuid": uuid,
                            "metadata": {
                                "telegram_reply": True,
                                "telegram_action": "edit" if name.endswith("edit_message") else "reply",
                            },
                        })
                        session_last_ts = ts or session_last_ts
                elif name == TG_REACT_TOOL and inp.get("emoji"):
                    # Reactions don't get their own assistant text — bolt onto the
                    # currently-buffered assistant entry as a sibling list.
                    if current_assistant is None:
                        current_assistant = {"ts": ts, "uuid": uuid, "texts": [], "reactions": []}
                    current_assistant.setdefault("reactions", []).append({
                        "emoji": inp["emoji"], "target_msg_id": inp.get("message_id"),
                    })
                elif name in ("Write", "Edit", "write", "edit", "NotebookEdit"):
                    fp = inp.get("file_path") or inp.get("path") or ""
                    if fp:
                        if current_assistant is None:
                            current_assistant = {"ts": ts, "uuid": uuid, "texts": [], "reactions": []}
                        current_assistant.setdefault("files", [])
                        if fp not in [f["path"] for f in current_assistant["files"]]:
                            current_assistant["files"].append({"path": fp, "action": name.lower()})

    flush_assistant()
    auto_name = next((m["content"][:80] for m in messages if m["role"] == "user" and m["content"]), "")
    return messages, auto_name, session_first_ts, session_last_ts


# ── Codex Adapter ──

def _parse_codex_session(jsonl_path):
    """Parse a Codex JSONL file into a list of unified messages."""
    messages = []
    session_first_ts = None
    session_last_ts = None

    for index, obj in enumerate(_iter_jsonl(jsonl_path)):
        t = obj.get("type")
        ts = obj.get("timestamp", "")
        payload = obj.get("payload", {})

        if t == "response_item" and payload.get("type") == "message":
            role = payload.get("role", "")
            content_blocks = payload.get("content", [])
            if not isinstance(content_blocks, list):
                continue

            if role == "user":
                texts = []
                for b in content_blocks:
                    if b.get("type") == "input_text":
                        txt = b.get("text", "").strip()
                        if txt and not txt.startswith("<environment_context>") and not txt.startswith("<permissions"):
                            texts.append(txt)
                if texts:
                    text = "\n".join(texts)
                    ts = _chat_input_sent_at(text, ts)
                    text = _strip_chat_input(text)
                    if ts:
                        if not session_first_ts:
                            session_first_ts = ts
                        session_last_ts = ts
                    messages.append({
                        "role": "user",
                        "sender": "user",
                        "content": text,
                        "ts": ts,
                        "message_type": "text",
                        "source_uuid": payload.get("id") or f"codex-message-{index}",
                        "metadata": {},
                    })

            elif role == "assistant":
                texts = []
                for b in content_blocks:
                    if b.get("type") == "output_text":
                        txt = b.get("text", "").strip()
                        if txt:
                            texts.append(txt)
                if texts:
                    text = "\n".join(texts)
                    text = _strip_oai_mem_citation(text)
                    if not text:
                        continue
                    if ts:
                        session_last_ts = ts
                    messages.append({
                        "role": "assistant",
                        "sender": "codex",
                        "content": text,
                        "ts": ts,
                        "message_type": "text",
                        "source_uuid": payload.get("id") or f"codex-message-{index}",
                        "metadata": {},
                    })

        elif t == "response_item" and payload.get("type") in ("function_call", "custom_tool_call"):
            name = payload.get("name", "")
            args = payload.get("arguments")
            if args is None:
                args = payload.get("input", "")
            if ts:
                session_last_ts = ts
            messages.append({
                "role": "assistant",
                "sender": "codex",
                "content": name,
                "ts": ts,
                "message_type": "tool_call",
                "source_uuid": payload.get("call_id") or payload.get("id"),
                "metadata": {"tool_name": name, "arguments": str(args)[:500]},
            })

    # skip developer-only sessions
    if not messages:
        return messages, "", None, None

    auto_name = ""
    for m in messages:
        if m["role"] == "user" and m["content"]:
            auto_name = m["content"][:80]
            break

    return messages, auto_name, session_first_ts, session_last_ts


def _codex_tool_summary(name, arguments):
    """Return a short visible activity label for a Codex function call."""
    args = _codex_tool_args(arguments)
    short_name = (name or "tool").rsplit(".", 1)[-1]
    patch_path = _codex_patch_path(arguments)
    if short_name == "exec_command":
        value = args.get("cmd") or ""
        label = "执行命令"
    elif short_name == "write_stdin":
        value = args.get("chars") or ""
        label = "输入终端"
    elif short_name == "apply_patch":
        value = patch_path or ""
        label = "修改文件"
    elif short_name == "view_image":
        value = args.get("path") or ""
        label = "查看图片"
    elif short_name == "update_plan":
        value = ""
        label = "更新计划"
    elif short_name == "parallel":
        calls = args.get("tool_uses") if isinstance(args, dict) else None
        value = f"{len(calls)} 个任务" if isinstance(calls, list) else ""
        label = "并行调用"
    elif short_name in ("open", "search_query", "find"):
        value = args.get("q") or args.get("pattern") or args.get("ref_id") or ""
        label = "浏览网页"
    elif short_name in ("image_query",):
        value = args.get("q") or ""
        label = "搜索图片"
    else:
        value = ""
        label = short_name.replace("_", " ")
    value = str(value).strip().replace("\n", " ")
    if len(value) > 72:
        value = value[:72] + "..."
    return label + ((" " + value) if value else "")


def _codex_tool_args(arguments):
    try:
        return json.loads(arguments or "{}") if isinstance(arguments, str) else (arguments or {})
    except json.JSONDecodeError:
        return {}


def _codex_patch_path(arguments):
    if not isinstance(arguments, str):
        return ""
    match = re.search(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", arguments, re.MULTILINE)
    return match.group(1).strip() if match else ""


_TG_INBOX_DIR = os.path.expanduser("~/.claude/channels/telegram/inbox")
_CODEX_IMAGE_PATH_RE = re.compile(
    r"@?("
    r"(?:/tmp/dashboard-uploads/[^\s<>\"']+"
    r"|" + re.escape(_TG_INBOX_DIR) + r"/[^\s<>\"']+"
    r"|" + re.escape(str(_INBOX_DIR)) + r"/[^\s<>\"']+"
    r"|/[^\s<>\"']+"
    r"|(?:\./)?[A-Za-z0-9._-]+/[^\s<>\"']+)"
    r"\.(?:png|jpe?g|gif|webp|svg|bmp|heic))",
    re.I,
)


def _codex_session_cwd(rows):
    for row in rows:
        if row.get("type") != "session_meta":
            continue
        cwd = ((row.get("payload") or {}).get("cwd") or "").strip()
        if cwd:
            return cwd
    return None


def _codex_local_image_resource(path, cwd=None):
    raw = path.lstrip("@")
    p = Path(raw)
    resolved = p if p.is_absolute() else Path(cwd or os.getcwd()) / raw
    try:
        resolved = resolved.resolve()
    except OSError:
        resolved = resolved.absolute()
    return {
        "type": "image",
        "src": "/api/files/download?path=" + quote(str(resolved), safe=""),
        "fname": resolved.name,
        "available": resolved.exists(),
    }


def _codex_live_text_blocks(text, cwd=None):
    """Render Codex image path references as attachment blocks."""
    blocks = []
    cursor = 0
    for match in _CODEX_IMAGE_PATH_RE.finditer(text):
        before = text[cursor:match.start()].strip()
        if before:
            blocks.append({"type": "text", "text": before})
        path = match.group(1)
        if "/tmp/dashboard-uploads/" in path:
            _path, upload_session, filename = _UPLOAD_RE.search(match.group(0)).groups()
            resource = _resources_from_content(match.group(0))[0]
            blocks.append({
                "type": "image",
                "src": f"/api/sessions/{resource['upload_session']}/uploads/{filename}",
                "fname": filename,
                "available": resource["available"],
            })
        elif "/.claude/channels/telegram/inbox/" in path:
            _path, filename = _TG_IMAGE_RE.search(match.group(0)).groups()
            resource = _resources_from_content(match.group(0))[0]
            blocks.append({
                "type": "image",
                "src": resource["serve_url"],
                "fname": filename,
                "available": resource["available"],
            })
        else:
            blocks.append(_codex_local_image_resource(path, cwd=cwd))
        cursor = match.end()
    tail = text[cursor:].strip()
    if tail:
        blocks.append({"type": "text", "text": tail})
    return blocks or [{"type": "text", "text": text}]


def read_codex_chat_messages(jsonl_path, limit=200, focus_uuid=None):
    """Render live Codex rollout events as the same blocks consumed by Chats UI."""
    rows = list(_iter_jsonl(jsonl_path))
    cwd = _codex_session_cwd(rows)
    tool_results = {
        (row.get("payload") or {}).get("call_id")
        for row in rows
        if row.get("type") == "response_item"
        and (row.get("payload") or {}).get("type") in ("function_call_output", "custom_tool_call_output")
    }
    last_started = -1
    last_complete = -1
    for index, row in enumerate(rows):
        if row.get("type") != "event_msg":
            continue
        event_type = (row.get("payload") or {}).get("type")
        if event_type == "task_started":
            last_started = index
        elif event_type == "task_complete":
            last_complete = index
    active_start = last_started if last_started > last_complete else None

    out = []
    tool_message = None
    current_turn_has_pending_tool = False

    for index, row in enumerate(rows):
        if row.get("type") == "event_msg":
            if (row.get("payload") or {}).get("type") in ("task_started", "task_complete"):
                tool_message = None
            continue
        if row.get("type") != "response_item":
            continue
        payload = row.get("payload") or {}
        payload_type = payload.get("type")
        ts = row.get("timestamp") or ""
        if payload_type == "message":
            role = payload.get("role") or ""
            if role not in ("user", "assistant"):
                continue
            if role in ("user", "assistant"):
                tool_message = None
            expected = "input_text" if role == "user" else "output_text"
            texts = [
                (block.get("text") or "").strip()
                for block in (payload.get("content") or [])
                if isinstance(block, dict) and block.get("type") == expected and (block.get("text") or "").strip()
            ]
            if role == "user":
                texts = [
                    text for text in texts
                    if not text.startswith("<environment_context>") and not text.startswith("<permissions")
                ]
            if texts:
                text = "\n".join(texts)
                if role == "assistant":
                    text = _strip_oai_mem_citation(text)
                    blocks = [{"type": "text", "text": text}]
                else:
                    ts = _chat_input_sent_at(text, ts)
                    text = _strip_chat_input(text)
                    blocks = _codex_live_text_blocks(text, cwd=cwd)
                if not text:
                    continue
                out.append({
                    "role": role,
                    "ts": ts,
                    # Codex live message payloads currently omit `id`. Keep a
                    # stable append-only row identity so frontend animations do
                    # not treat old replies as new when the visible window shifts.
                    "source_uuid": payload.get("id") or f"codex-message-{index}",
                    "blocks": blocks,
                })
        elif payload_type in ("function_call", "custom_tool_call"):
            call_id = payload.get("call_id") or payload.get("id") or f"tool-{index}"
            is_current = active_start is not None and index > active_start
            done = call_id in tool_results or not is_current
            if is_current and not done:
                current_turn_has_pending_tool = True
            if tool_message is None:
                tool_message = {
                    "role": "assistant",
                    "ts": ts,
                    "source_uuid": "tools-" + str(call_id),
                    "blocks": [{"type": "tool_group", "tools": []}],
                }
                out.append(tool_message)
            name = payload.get("name") or "tool"
            short_name = name.rsplit(".", 1)[-1]
            raw_args = payload.get("arguments")
            if raw_args is None:
                raw_args = payload.get("input")
            tool_entry = {
                "id": call_id,
                "name": short_name,
                "summary": _codex_tool_summary(name, raw_args),
                "done": done,
            }
            args = _codex_tool_args(raw_args)
            fp = (args or {}).get("path") or (args or {}).get("file_path") or ""
            if short_name == "apply_patch":
                fp = _codex_patch_path(raw_args) or fp
            if fp and short_name in ("write_file", "apply_diff", "create_file", "apply_patch"):
                tool_entry["file"] = {"path": fp, "name": os.path.basename(fp), "action": short_name}
            tool_message["blocks"][0]["tools"].append(tool_entry)

    if active_start is not None and not current_turn_has_pending_tool:
        out.append({
            "role": "assistant",
            "ts": rows[-1].get("timestamp") if rows else "",
            "source_uuid": "codex-thinking",
            "blocks": [{
                "type": "tool_group",
                "tools": [{"id": "inflight", "name": "thinking", "summary": "正在思考...", "done": False}],
            }],
        })

    if not limit or len(out) <= limit:
        return out
    if focus_uuid:
        target = next((i for i, message in enumerate(out) if message.get("source_uuid") == focus_uuid), None)
        if target is not None:
            start = max(0, target - max(1, limit // 2))
            return out[start:start + limit]
    return out[-limit:]


# ── Import ──

def import_cc_sessions(conn=None, force=False):
    """Scan and import all CC JSONL sessions into the unified store."""
    def _do(conn):
        imported = 0
        for d in _cc_session_dirs():
            if not d.exists():
                continue
            for jf in d.glob("*.jsonl"):
                sid = jf.stem
                existing = conn.execute(
                    "SELECT updated_at FROM sessions WHERE id=? AND source='cc'", (sid,)
                ).fetchone()
                file_mtime = datetime.fromtimestamp(jf.stat().st_mtime).isoformat()
                if existing and existing["updated_at"] == file_mtime and not force:
                    continue

                msgs, auto_name, first_ts, last_ts = _parse_cc_session(jf)
                if not msgs:
                    if existing:
                        conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
                    continue

                conn.execute("""
                    INSERT INTO sessions (id, source, auto_name, created_at, updated_at)
                    VALUES (?, 'cc', ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        auto_name=excluded.auto_name,
                        updated_at=excluded.updated_at
                """, (sid, auto_name, first_ts, file_mtime))

                conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
                for m in msgs:
                    conn.execute("""
                        INSERT INTO messages
                            (session_id, role, sender, content, ts, source,
                             message_type, status, metadata, source_uuid)
                        VALUES (?, ?, ?, ?, ?, 'cc', ?, 'delivered', ?, ?)
                    """, (
                        sid, m["role"], m["sender"], m["content"], m["ts"],
                        m["message_type"], json.dumps(m["metadata"], ensure_ascii=False),
                        m["source_uuid"],
                    ))
                imported += 1
        conn.commit()
        return imported

    if conn:
        return _do(conn)
    with get_db() as c:
        return _do(c)


def _physical_cc_session_ids():
    ids = set()
    for d in _cc_session_dirs():
        if d.exists():
            ids.update(jf.stem for jf in d.glob("*.jsonl"))
    return ids


def _record_sender(value, role):
    name = (value or "").strip().lower()
    if role == "user":
        return "user"
    if role == "assistant":
        return "assistant"
    return name or "system"


def import_cc_record_sessions(conn=None, force=False):
    """Import durable chat_records exports for CC sessions whose JSONL is gone."""
    def _do(conn):
        imported = 0
        physical_ids = _physical_cc_session_ids()
        for record_path in _iter_cc_record_files():
            data = _load_cc_record(record_path)
            if not data:
                continue
            sid = data.get("session_id")
            if not sid or sid in physical_ids:
                continue
            messages = [
                m for m in (data.get("messages") or [])
                if isinstance(m, dict) and (m.get("content") or "").strip()
            ]
            if not messages:
                continue
            file_mtime = datetime.fromtimestamp(record_path.stat().st_mtime).isoformat()
            existing = conn.execute(
                "SELECT updated_at FROM sessions WHERE id=? AND source='cc'", (sid,)
            ).fetchone()
            if existing and existing["updated_at"] == file_mtime and not force:
                continue

            first_ts = data.get("created_at") or messages[0].get("ts") or ""
            last_ts = data.get("updated_at") or messages[-1].get("ts") or ""
            metadata = {
                "record_source": str(record_path),
                "record_exported_at": data.get("exported_at") or "",
                "logical_thread": data.get("logical_thread") or "",
            }
            conn.execute("""
                INSERT INTO sessions (id, source, name, auto_name, created_at, updated_at, metadata)
                VALUES (?, 'cc', ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source='cc',
                    name=excluded.name,
                    auto_name=excluded.auto_name,
                    updated_at=excluded.updated_at,
                    metadata=excluded.metadata
            """, (
                sid, data.get("name") or sid[:8], data.get("name") or sid[:8],
                first_ts, file_mtime, json.dumps(metadata, ensure_ascii=False),
            ))
            conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
            for index, message in enumerate(messages):
                role = message.get("role") or "user"
                source_uuid = message.get("source_uuid") or f"record:{sid}:{index}"
                msg_meta = {
                    "record_source": str(record_path),
                    "record_index": index,
                }
                conn.execute("""
                    INSERT INTO messages
                        (session_id, role, sender, content, ts, source,
                         message_type, status, metadata, source_uuid)
                    VALUES (?, ?, ?, ?, ?, 'cc', ?, 'delivered', ?, ?)
                """, (
                    sid,
                    role,
                    _record_sender(message.get("sender"), role),
                    message.get("content") or "",
                    message.get("ts") or "",
                    message.get("message_type") or "text",
                    json.dumps(msg_meta, ensure_ascii=False),
                    source_uuid,
                ))
            imported += 1
        conn.commit()
        return imported

    if conn:
        return _do(conn)
    with get_db() as c:
        return _do(c)


def import_codex_sessions(conn=None, force=False):
    """Scan and import all Codex JSONL sessions into the unified store."""
    def _do(conn):
        imported = 0
        if not CODEX_SESSIONS_DIR.exists():
            return 0
        for jf in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
            sid_match = re.search(r'rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$', jf.name)
            if not sid_match:
                continue
            sid = "codex-" + sid_match.group(1)

            existing = conn.execute(
                "SELECT updated_at FROM sessions WHERE id=? AND source='codex'", (sid,)
            ).fetchone()
            file_mtime = datetime.fromtimestamp(jf.stat().st_mtime).isoformat()
            if existing and existing["updated_at"] == file_mtime and not force:
                continue

            msgs, auto_name, first_ts, last_ts = _parse_codex_session(jf)
            if not msgs:
                if existing:
                    conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
                continue

            conn.execute("""
                INSERT INTO sessions (id, source, auto_name, created_at, updated_at)
                VALUES (?, 'codex', ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    auto_name=excluded.auto_name,
                    updated_at=excluded.updated_at
            """, (sid, auto_name, first_ts, file_mtime))

            conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
            for m in msgs:
                conn.execute("""
                    INSERT INTO messages
                        (session_id, role, sender, content, ts, source,
                         message_type, status, metadata, source_uuid)
                    VALUES (?, ?, ?, ?, ?, 'codex', ?, 'delivered', ?, ?)
                """, (
                    sid, m["role"], m["sender"], m["content"], m["ts"],
                    m["message_type"], json.dumps(m["metadata"], ensure_ascii=False),
                    m["source_uuid"],
                ))
            imported += 1
        conn.commit()
        return imported

    if conn:
        return _do(conn)
    with get_db() as c:
        return _do(c)


def import_all(force=False):
    """Import from all sources. Returns counts per source."""
    with get_db() as conn:
        cc = import_cc_sessions(conn, force=force)
        cc_records = import_cc_record_sessions(conn, force=force)
        codex = import_codex_sessions(conn, force=force)
    return {"cc": cc, "cc_records": cc_records, "codex": codex}


# ── Query ──

def list_sessions(source=None, scope="active", limit=50, offset=0):
    with get_db() as conn:
        where = []
        params = []
        if source:
            where.append("s.source = ?")
            params.append(source)
        if scope == "active":
            where.append("s.hidden = 0 AND s.deleted = 0")
        elif scope == "trash":
            where.append("s.hidden = 1 AND s.deleted = 0")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(f"""
            SELECT s.*,
                   COUNT(m.id) as message_count,
                   MAX(m.ts) as last_message_ts
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id AND m.message_type = 'text'
            {where_sql}
            GROUP BY s.id
            ORDER BY last_message_ts DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()
        return [dict(r) for r in rows]


def get_session_messages(session_id, limit=500, offset=0, message_type=None):
    with get_db() as conn:
        where = ["session_id = ?"]
        params = [session_id]
        if message_type:
            where.append("message_type = ?")
            params.append(message_type)
        where_sql = " AND ".join(where)
        rows = conn.execute(f"""
            SELECT * FROM messages
            WHERE {where_sql}
            ORDER BY ts ASC, id ASC
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()
        return [dict(r) for r in rows]


_URL_RE = re.compile(
    r"(?<![@/\w.-])("
    r"(?:https?://|www\.)[^\s<>\"']+"
    r"|(?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|app|ai|cn|co|me|tech|xyz)"
    r"(?:/[^\s<>\"']*)?"
    r")",
    re.IGNORECASE,
)
_UPLOAD_RE = re.compile(r"@?(/tmp/dashboard-uploads/([^/\s]+)/([^\s<>\"']+))", re.I)
# Absolute paths the Telegram MCP plugin pastes when forwarding photo messages
# (CC JSONLs include the on-disk path, not a URL; we resolve via /api/telegram/inbox/).
# Built off $HOME so the regex works for any user who installs Prism.
_TG_IMAGE_RE = re.compile(
    r"@?(" + re.escape(_TG_INBOX_DIR) + r"/([^\s<>\"']+\.(?:png|jpg|jpeg|gif|webp|heic)))",
    re.I,
)
_IMG_EXT_RE = re.compile(r'\.(png|jpg|jpeg|gif|webp|svg|bmp|heic)$', re.I)
_FILE_EXT_RE = re.compile(r'\.(pdf|doc|docx|xls|xlsx|ppt|pptx|zip|tar|gz|md|html|csv|txt|py|js|ts|json|yaml|yml|sh)$', re.I)
_CJK_RE = re.compile(r"[㐀-鿿]")


def _resources_from_content(content):
    """Extract attachments and links while keeping their source message."""
    resources = []
    for path, upload_session, filename in _UPLOAD_RE.findall(content or ""):
        resolved_path = path
        resolved_session = upload_session
        if not os.path.exists(resolved_path):
            matches = list(Path("/tmp/dashboard-uploads").glob("*/" + filename))
            if matches:
                resolved_path = str(matches[0])
                resolved_session = matches[0].parent.name
        resources.append({
            "kind": "image" if _IMG_EXT_RE.search(filename) else "file",
            "path": resolved_path,
            "upload_session": resolved_session,
            "filename": filename,
            "available": os.path.exists(resolved_path),
        })
    for path, filename in _TG_IMAGE_RE.findall(content or ""):
        resources.append({
            "kind": "image", "path": path, "filename": filename,
            "available": os.path.exists(path),
            "serve_url": "/api/telegram/inbox/" + filename,
            "source": "telegram",
        })
    for url in _URL_RE.findall(content or ""):
        url = url.rstrip('.,，。)）]】')
        if not re.match(r"https?://", url, re.IGNORECASE):
            url = "https://" + url
        resources.append({"kind": "link", "url": url})
    return resources


_IMG_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.ico'}

def _resources_from_metadata_files(metadata_str):
    """Extract file resources from metadata.files (set by CC tool_use detection)."""
    resources = []
    try:
        meta = json.loads(metadata_str) if isinstance(metadata_str, str) else (metadata_str or {})
    except (json.JSONDecodeError, TypeError):
        return resources
    for f in meta.get("files", []):
        fp = f.get("path", "")
        if not fp:
            continue
        fname = os.path.basename(fp)
        ext = os.path.splitext(fname)[1].lower()
        kind = "image" if ext in _IMG_EXTS else "file"
        resources.append({
            "kind": kind,
            "path": fp,
            "filename": fname,
            "available": os.path.isfile(fp),
            "serve_url": "/api/files/download?path=" + fp,
            "action": f.get("action", "write"),
        })
    return resources

def _detect_content_type(content):
    resources = _resources_from_content(content)
    if any(r["kind"] == "image" for r in resources) or 'image_path' in content:
        return 'image'
    if any(r["kind"] == "file" for r in resources):
        return 'file'
    if any(r["kind"] == "link" for r in resources):
        return 'link'
    return 'text'


def _fts_phrase_query(query):
    query = " ".join((query or "").strip().split())
    if not query:
        return None
    return '"' + query.replace('"', '""') + '"'


def _use_substring_search(query):
    return bool(query and _CJK_RE.search(query))


def search_messages(query, source=None, content_type=None, session_id=None,
                    additional_session_ids=None, limit=50, offset=0):
    with get_db() as conn:
        use_substring = _use_substring_search(query)
        fts_query = None if use_substring else _fts_phrase_query(query)
        where = ["m.message_type = 'text'"]
        params = []
        if use_substring:
            where.append("m.content LIKE ?")
            params.append(f"%{query}%")
        elif fts_query:
            where.append("messages_fts MATCH ?")
            params.append(fts_query)
        if source:
            where.append("m.source = ?")
            params.append(source)
        if session_id:
            session_ids = [session_id] + [
                value for value in (additional_session_ids or [])
                if value and value != session_id
            ]
            where.append("m.session_id IN (" + ",".join("?" for _ in session_ids) + ")")
            params.extend(session_ids)
        if content_type and content_type != 'all':
            if content_type == 'image':
                where.append("""(
                    LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.jpg%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.jpeg%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.png%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.gif%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.webp%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.heic%'
                )""")
            elif content_type == 'file':
                where.append("""(
                    LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.pdf%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.doc%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.docx%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.xls%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.xlsx%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.ppt%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.pptx%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.zip%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.tar.gz%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.csv%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.txt%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.md%'
                    OR LOWER(m.content) LIKE '%/tmp/dashboard-uploads/%.json%'
                ) AND LOWER(m.content) NOT LIKE '%/tmp/dashboard-uploads/%.jpg%'
                  AND LOWER(m.content) NOT LIKE '%/tmp/dashboard-uploads/%.png%'
                  AND LOWER(m.content) NOT LIKE '%/tmp/dashboard-uploads/%.jpeg%'""")
            elif content_type == 'link':
                where.append("has_link(m.content) = 1")
        where_sql = " AND ".join(where)
        join_fts = "JOIN messages_fts ON messages_fts.rowid = m.id" if fts_query else ""
        try:
            rows = conn.execute(f"""
                SELECT m.*, s.name AS session_name, s.auto_name AS session_auto_name
                FROM messages m
                {join_fts}
                LEFT JOIN sessions s ON m.session_id = s.id
                WHERE {where_sql}
                ORDER BY m.ts DESC
                LIMIT ? OFFSET ?
            """, params + [limit, max(0, int(offset or 0))]).fetchall()
        except sqlite3.Error:
            if not query:
                raise
            fallback_where = [part for part in where if part != "messages_fts MATCH ?"]
            fallback_params = [p for p in params if p != fts_query]
            fallback_where.append("m.content LIKE ?")
            fallback_params.append(f"%{query}%")
            rows = conn.execute(f"""
                SELECT m.*, s.name AS session_name, s.auto_name AS session_auto_name
                FROM messages m
                LEFT JOIN sessions s ON m.session_id = s.id
                WHERE {" AND ".join(fallback_where)}
                ORDER BY m.ts DESC
                LIMIT ? OFFSET ?
            """, fallback_params + [limit, max(0, int(offset or 0))]).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["resources"] = _resources_from_content(item.get("content", "")) + _resources_from_metadata_files(item.get("metadata"))
            item["content_type"] = _detect_content_type(item.get("content", ""))
            if not item["content_type"] or item["content_type"] == "text":
                meta_res = _resources_from_metadata_files(item.get("metadata"))
                if any(r["kind"] == "file" for r in meta_res):
                    item["content_type"] = "file"
                elif any(r["kind"] == "image" for r in meta_res):
                    item["content_type"] = "image"
            results.append(item)
        return results


def get_stats():
    with get_db() as conn:
        total_sessions = conn.execute("SELECT COUNT(*) FROM sessions WHERE deleted=0").fetchone()[0]
        total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        by_source = conn.execute("""
            SELECT source, COUNT(DISTINCT session_id) as sessions, COUNT(*) as messages
            FROM messages GROUP BY source
        """).fetchall()
        return {
            "total_sessions": total_sessions,
            "total_messages": total_messages,
            "by_source": [dict(r) for r in by_source],
        }


# ── Block-format compatibility (for existing frontend) ──

def get_session_as_blocks(session_id, limit=500, focus_id=None):
    """Return recent chat blocks, or a window centered on a search hit."""
    with get_db() as conn:
        if focus_id is not None:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id=? AND message_type='text' ORDER BY ts ASC, id ASC",
                (session_id,),
            ).fetchall()
            all_messages = [dict(r) for r in rows]
            target_index = next((i for i, m in enumerate(all_messages) if m["id"] == focus_id), len(all_messages) - 1)
            start = max(0, target_index - max(1, limit // 2))
            msgs = all_messages[start:start + limit]
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id=? AND message_type='text' ORDER BY ts DESC, id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            msgs = [dict(r) for r in reversed(rows)]
    blocks = []
    # Walk attachments + Telegram-inbox images in one pass — the body may contain
    # either, and we want the rendered order to follow the actual text positions.
    combined_re = re.compile(
        _UPLOAD_RE.pattern + "|" + _TG_IMAGE_RE.pattern,
        _UPLOAD_RE.flags,
    )
    for m in msgs:
        if m["message_type"] != "text":
            continue
        content = m["content"]
        message_blocks = []
        cursor = 0
        for match in combined_re.finditer(content):
            text = content[cursor:match.start()].strip()
            if text:
                message_blocks.append({"type": "text", "text": text})
            if "/tmp/dashboard-uploads/" in match.group(0):
                upload_match = _UPLOAD_RE.search(match.group(0))
                if not upload_match:
                    cursor = match.end()
                    continue
                _path, upload_session, filename = upload_match.groups()
                resource = _resources_from_content(match.group(0))[0]
                if _IMG_EXT_RE.search(filename):
                    message_blocks.append({
                        "type": "image",
                        "src": f"/api/sessions/{resource['upload_session']}/uploads/{filename}",
                        "fname": filename,
                        "available": resource["available"],
                    })
                else:
                    message_blocks.append({"type": "text", "text": "附件: " + filename})
            elif "/.claude/channels/telegram/inbox/" in match.group(0):
                tg_match = _TG_IMAGE_RE.search(match.group(0))
                if not tg_match:
                    cursor = match.end()
                    continue
                _path, filename = tg_match.groups()
                resource = _resources_from_content(match.group(0))[0]
                message_blocks.append({
                    "type": "image",
                    "src": resource["serve_url"],
                    "fname": filename,
                    "available": resource["available"],
                })
            cursor = match.end()
        tail = content[cursor:].strip()
        if tail:
            message_blocks.append({"type": "text", "text": tail})
        if not message_blocks:
            message_blocks = [{"type": "text", "text": content}]
        # Surface the "this message went to Telegram" delivery marker so the UI
        # can show a little badge under assistant bubbles that came via the bot.
        try:
            metadata = json.loads(m.get("metadata") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if metadata.get("telegram_reply"):
            label = "已编辑 Telegram 消息" if metadata.get("telegram_action") == "edit" else "已回复到 Telegram"
            message_blocks.append({"type": "delivery", "channel": "telegram", "label": label})
        blocks.append({
            "id": m["id"],
            "source_uuid": m["source_uuid"],
            "role": m["role"],
            "ts": m["ts"],
            "blocks": message_blocks,
        })
    return blocks
