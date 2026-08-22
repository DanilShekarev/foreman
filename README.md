# Foreman

**English | [Русский](README.ru.md)**

Self-hosted workspace for small teams: chat, a git-backed knowledge base, a
kanban board, voice calls with transcripts — and an AI agent that maintains
your docs and answers questions by actually reading your project's source code.

Everything lives on your server. One SQLite file, one git repository for the
docs, a frontend with zero build steps and zero CDN calls.

> ⚠️ The UI is currently Russian-only. English localization is the first big
> item on the roadmap after the initial release.

<!-- screenshots: docs/img/chat.png, docs/img/vault.png, docs/img/kanban.png -->

## What's inside

- **Team chat** — channels, DMs, threads, reactions, pins, read receipts,
  mentions, file attachments, paste-a-screenshot.
- **Knowledge base ("vault")** — markdown documents under git: wiki-links,
  backlinks, in-browser editor (WYSIWYG or CodeMirror), full history, one-click
  rollback, Word import/export.
- **AI agent** — lives in the chat. Maintains the vault, answers "how does this
  actually work?" by reading a read-only mirror of your code repository. Every
  turn it makes is a git commit with a visible diff and a rollback button.
- **Kanban board** — Trello-style tasks with drag & drop, checklists,
  priorities and labels; the agent can read and update it too.
- **Calls** — WebRTC voice calls with screen sharing; each speaker's track is
  recorded and can be transcribed into a thread by an external ASR pipeline.

## Agent engines

Two interchangeable ways to run the agent, configurable per-chat in the admin
UI ("Модели агента"):

| Engine | What you need |
|---|---|
| **Claude** (Agent SDK) | A Claude subscription: Node.js + [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code), then `claude setup-token` → save the token to `auth_token.txt` |
| **OpenAI-compatible** | Any endpoint: OpenRouter, a local LLM server, etc. Just a base URL + API key in the admin UI |

The server runs fine with neither — you get chat, vault and kanban without
the agent.

## Quick start

Requirements: **Python 3.10+** and **git** in `PATH`.

```bash
git clone https://github.com/DanilShekarev/foreman.git
cd foreman
./start.sh        # Windows: start.bat
```

The first run creates a virtualenv, installs dependencies, seeds the database
and prints the admin password to the console. Open http://127.0.0.1:8010 and
log in as `admin`.

All settings are optional and live in `.env` (copy `.env.example` and edit).

## Letting the agent read your code

Point Foreman at any local git repository:

```ini
# .env
GD_CODE_REPO=/path/to/your/project
GD_CODE_PATHS=src;docs        # optional: limit the checkout to subfolders
```

The agent never sees your working copy. Foreman maintains a shallow, partial
**mirror** of the branch (`GD_CODE_BRANCH`, default `main`): only committed
changes, refreshed every `GD_MIRROR_SYNC_MIN` minutes. Enable two local
options in the *source* repository so the mirror stays small:

```bash
git config uploadpack.allowFilter true
git config uploadpack.allowAnySHA1InWant true
```

Then fill in two documents in the vault root (seeded as templates on first
run): `PROJECT.md` — what your team does, in your own words; `CODE_MAP.md` —
a map of the repository. Both are read by the agent at the start of every
conversation and are edited right in the web UI.

## Keeping the agent on a short leash

The agent's permissions are enforced technically, not by prompt politeness:

- **No shell.** Bash is disabled entirely; git commits are made by the server,
  not the agent.
- **Tool allow-list.** Reading, searching and text editing only — no web, no
  downloads, no subagents.
- **Path sandbox.** Write access: the shared vault folder (plus the owner's
  personal folder in private sessions). Code mirror: read-only. All path rules
  live in one module (`vaultlib.py`) and are shared by the agent guards, the
  HTTP API and the UI.
- **Everything is reversible.** Each agent turn commits only the files it
  touched; the diff is shown in the chat with a one-click rollback.

## Optional integrations

- **pandoc** — enables export of vault documents to `.docx`.
- **Call transcription** — an external ASR pipeline directory
  (`GD_TRANSCRIBE_DIR`); absent — calls still work, transcription is silently
  off.
- **TURN server** — for calls across strict NATs (`GD_TURN_URL` etc.);
  without it calls work on LAN and simple networks. Calls require HTTPS
  (a reverse proxy such as Caddy) — on plain http the call button is hidden.

## Configuration reference

See [.env.example](.env.example) — every variable is documented there.
Highlights:

| Variable | Default | Meaning |
|---|---|---|
| `GD_HOST` / `GD_PORT` | `127.0.0.1` / `8010` | Listen address |
| `GD_WEB_BASE` | — | Path prefix behind a reverse proxy |
| `GD_ADMIN_PASSWORD` | random, printed once | First-run admin password |
| `GD_DB` / `GD_VAULT` | `data/gd.sqlite` / `vault/` | Data locations |
| `GD_CODE_REPO` | — | Git repo the agent may read (empty = no code) |
| `GD_CLAUDE_TOKEN_FILE` | `auth_token.txt` | Claude subscription token |

## Architecture in one paragraph

FastAPI + SQLite (WAL) + vanilla JS. One uvicorn worker, no reload — agent
sessions live in-process. Agent streaming is NDJSON over POST; other clients
poll a change feed. The vault is a separate git repository; every write goes
through a single lock. The only WebSocket is call signaling — media flows
peer-to-peer. A deeper tour lives in [CLAUDE.md](CLAUDE.md) (written as
context for AI coding agents, works great for humans too).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Vendored frontend libraries are listed
in [THIRD-PARTY.md](THIRD-PARTY.md).

## License

[AGPL-3.0](LICENSE). In short: use and self-host freely; if you
modify Foreman and offer it to others over a network, you must publish
your modified source under the same license.
