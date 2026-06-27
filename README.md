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
- **Telegram channel** *(optional)* — mount the official `claude-plugins-official/telegram` MCP
  plugin onto a Claude Code session so DMs to your bot land in chat. Two CC panes can't share the
  same bot, so Prism auto-strips the channel from the previous holder when you mount it elsewhere.
  See [Telegram channel](#telegram-channel-optional) below.
- **Sketch theme + dark mode** — a pencil-on-paper ("素描稿纸") visual theme, with a dark variant
  (charcoal paper + cream pencil) plus a system-following appearance toggle in *Settings → Appearance*.

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

## Telegram channel (optional)

Prism can ride on top of Anthropic's official Telegram MCP plugin so the bot's DMs become messages
in your chat tab. The plugin (not Prism) does the actual TG protocol work; Prism handles UI
plumbing and **single-holder isolation** — at most one Claude Code pane can poll the bot at a time,
so Prism transparently strips the channel from the previous holder when a new one mounts it.

**One-time setup:**

1. **Get a bot token** from [@BotFather](https://t.me/BotFather) on Telegram. Put it in `.env`:

   ```env
   TELEGRAM_BOT_TOKEN=123456789:AAH...
   ```

2. **Install the plugin** in your Claude Code:

   ```
   /plugin marketplace add anthropic/claude-plugins-official
   /plugin install telegram@claude-plugins-official
   ```

3. **Restart Prism** so it picks up the env var, then in *Code → ＋ (New session)* tick
   **接入 Telegram plugin** when you start a Claude session. Prism will start that session with
   `--channels plugin:telegram@claude-plugins-official` so inbound DMs route into chat.

4. **Pair**: in Telegram, DM your bot anything once — it replies with a 6-character pairing code.
   In the matching Claude Code session run:

   ```
   /telegram:access pair <code>
   ```

   From then on, DMs to the bot land as user messages in this session's chat, and the assistant
   can `mcp__plugin_telegram__reply` back to TG. Prism renders both halves like a normal chat.

**Switching holders:** If you have multiple CC sessions and want a *different* one to own the bot,
just create the new session with **接入 Telegram plugin** ticked — Prism rebuilds the old holder
without the channel and isolates it from re-grabbing the bot via project `enabledPlugins`. No
manual `kill-session` dance needed.

**Heads-up:** ticking the box is what wires up **inbound DM routing**. If a session merely
auto-loads the plugin via a project's `.claude/settings.json` `enabledPlugins`, the assistant can
*send* TG messages but DMs won't show up in chat — and worse, that session can silently steal the
poller from the real holder. The toggle (or Prism's auto-isolation when you don't tick it) is what
prevents that.

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

## Recent updates

- **Dark mode (system-following)** — *Settings → Appearance* adds 跟随系统 / 浅色 / 深色. The
  dark variant is a charcoal-paper sketch with cream pencil that follows `prefers-color-scheme`;
  iOS Safari `<meta theme-color>` is replaced (not just attribute-mutated) on flips so the status
  bar tint actually updates.
- **Telegram channel** — official `claude-plugins-official/telegram` plugin support: new-session
  toggle, single-holder auto-isolation, `/api/telegram/inbox/{fname}` for bot-uploaded images,
  delivery markers (`已回复到 Telegram` badge) under assistant bubbles, search snippets strip the
  inbox path, image preview supports the inbox URL.
- **AskUserQuestion protocol** — rewired through `/api/sessions/{name}/terminal-respond` with the
  `{ submit_after_review: true }` server-side wait, fixing the "最后确认不是我点的选项" race plus the
  multi-select "Type something" desync (`Down × N` lands on the field, then `Tab` + `Enter`).
- **Chat rendering polish** — `sent_at` baked into outgoing chat-input for precise receipt match,
  receipt polling extended to 2 minutes, reveal animation capped at 40 messages / 1200 chars,
  blocking-prompt 409 surfaces a `blocked` pending state, terminal-prompt fingerprint includes
  body text so Codex prompts that only change wording re-render correctly.
