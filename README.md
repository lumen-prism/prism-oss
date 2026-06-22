# Prism

A multi-CLI **chat frontend** — render Claude Code / Codex / OpenCode / Shell agent sessions as
clean chat conversations, with a live terminal, full markdown + syntax highlighting, and a usage meter.

This is the open-source public subset of the Prism dashboard. It ships three views — **Chats**,
**Code**, and **Settings** — plus a token-**usage** pill. Private/personal features are not included.

## Features

- **Code** — a list of your live CLI agent sessions (Claude Code, Codex, OpenCode, Shell) rendered
  as chat threads, with a real-time `xterm.js` terminal, model/effort switching, new-session
  creation, image attach + crop/draw, and archived-session browsing.
- **Chats** — a chat UI shell (message stream, optimistic send, live SSE updates, tool-call /
  thinking / ask-card rendering).
- **Search** — full-text search across all sessions (SQLite FTS5, with substring matching for CJK),
  scoped to all records or the current chat, with paginated results.
- **Markdown + code rendering** — headings, lists, task lists, tables, quotes, autolinks, inline
  code pills, and fenced code blocks with `highlight.js` syntax highlighting + copy/expand.
- **Usage** — a top usage pill and a Settings panel showing remaining Claude Code / Codex quota
  windows.
- **Sketch theme** — a single pencil-on-paper ("素描稿纸") visual theme.

## Stack

- **Backend**: FastAPI (`server.py`) + helper modules (`terminal_manager.py`, `message_store.py`).
  Token-based auth with a single dashboard password.
- **Frontend**: vanilla HTML/CSS/JS in `static/` — no build step.
- **Vendored**: `highlight.js`, `xterm.js`, `cropper.js` under `static/vendor/`.

## Setup

```bash
# 1. install deps
pip install -r requirements.txt

# 2. configure
cp .env.example .env
#   edit .env and set DASHBOARD_PASSWORD

# 3. run
python3 server.py            # serves on http://0.0.0.0:8001 (set PORT to change)
```

Open the server URL, log in with your `DASHBOARD_PASSWORD`, and you'll land on the **Code** view.

### Environment

| Variable             | Required | Default                     | Notes                                          |
| -------------------- | -------- | --------------------------- | ---------------------------------------------- |
| `DASHBOARD_PASSWORD` | yes      | —                           | login password (any string you choose)         |
| `PORT`               | no       | `8001`                      | bind port                                      |
| `PRISM_DATA_DIR`     | no       | `~/.local/share/prism`      | where chat/message data is stored              |
| `PRISM_INBOX_DIR`    | no       | (neutral default)           | optional inbound-file directory                |

### The usage pill (optional)

The `/api/usage` endpoint reads your **own** local CLI OAuth credentials at runtime to show
remaining quota — `~/.claude/.credentials.json` (Claude Code) and `~/.codex/auth.json` (Codex).
The app never stores or transmits these; if the files are absent the usage pill just shows nothing
and everything else keeps working.

## Project layout

```
server.py             FastAPI app — auth + chat/code/usage REST + SSE endpoints
terminal_manager.py   live PTY/session manager; CLI-session discovery & parsing
message_store.py      SQLite-backed message/session store + full-text search
static/
  app.html            single-page app (inline CSS + bootstrap script)
  chat-page.js        chats + code + usage rendering
  chat-md.js          markdown renderer
  chat-code.js        code-block / copy helper
  vendor/             highlight.js, xterm.js, cropper.js
```

## Notes

- CLI session discovery scans the standard locations (e.g. `~/.claude/projects` for Claude Code).
- This is a non-commercial personal project. UI deliberately pays homage to each CLI's official app.
- License: [MIT](LICENSE).
