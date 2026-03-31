# Architecture

## Overview

OpenCopilot is a cloud-native application that lets users run GitHub Copilot agents via a **web dashboard**, **REST/WebSocket API**, or **Telegram bot**. It wraps the standalone `copilot` CLI (`@github/copilot`) with session management, file storage, real-time streaming, and a custom agent/skill system — all deployed on Azure.

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Clients                                    │
│                                                                     │
│   ┌──────────────┐   ┌──────────────┐   ┌────────────────────────┐ │
│   │ Web Dashboard │   │ REST / WS    │   │ Telegram Bot           │ │
│   │ (frontend/)   │   │ Clients      │   │ (@BotFather webhook)   │ │
│   └──────┬───────┘   └──────┬───────┘   └───────────┬────────────┘ │
└──────────┼──────────────────┼───────────────────────┼──────────────┘
           │                  │                       │
           ▼                  ▼                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     FastAPI Backend (port 8000)                      │
│                                                                     │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────┐ ┌────────┐ ┌─────┐ │
│  │ /chat   │ │ /agents │ │ /files  │ │/logs │ │/skills │ │/tg  │ │
│  │ Router  │ │ Router  │ │ Router  │ │Router│ │Router  │ │Route│ │
│  └────┬────┘ └────┬────┘ └────┬────┘ └──┬───┘ └───┬────┘ └──┬──┘ │
│       │           │           │         │         │          │     │
│  ┌────▼────┐ ┌────▼─────┐ ┌──▼──────┐  │    ┌────▼─────┐ ┌─▼──┐ │
│  │Copilot  │ │Agent     │ │Blob     │  │    │Agent     │ │Tg  │ │
│  │Service  │ │Parser    │ │Storage  │  │    │Parser    │ │Bot │ │
│  ├─────────┤ └──────────┘ └─────────┘  │    └──────────┘ └────┘ │
│  │Session  │                           │                         │
│  │Manager  │    Response Parser        │                         │
│  └─────────┘                           │                         │
│       │                                │                         │
│       ▼                                │                         │
│  ┌──────────────────┐   ┌──────────────▼──┐                     │
│  │ copilot CLI       │   │ In-Memory Log   │                     │
│  │ (@github/copilot) │   │ Buffer + Queues │                     │
│  └──────────────────┘   └─────────────────┘                     │
└─────────────────────────────────────────────────────────────────────┘
           │                                    │
           ▼                                    ▼
┌─────────────────────┐            ┌─────────────────────┐
│  GitHub Copilot     │            │  Azure Blob Storage  │
│  (AI models)        │            │  (files, sessions)   │
└─────────────────────┘            └─────────────────────┘
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, Uvicorn |
| Frontend | Vanilla HTML/CSS/JS, WebSocket streaming |
| AI | GitHub Copilot via standalone **`copilot`** CLI (`@github/copilot` npm package) |
| Storage | Azure Blob Storage (prod) / local filesystem (dev) |
| Auth | Azure Entra ID (JWT, RS256) — optional |
| Chat Platform | Telegram Bot API (webhook) |
| Deployment | Docker → Azure Container Registry → Azure Container Apps |

## Project Structure

```
├── backend/
│   └── app/
│       ├── main.py              # FastAPI app, CORS, static files, startup
│       ├── config.py            # Settings from environment variables
│       ├── auth.py              # Azure AD JWT verification
│       ├── models/
│       │   ├── agent.py         # AgentSummary, AgentDetail
│       │   ├── chat.py          # ChatSession, ChatMessage, MessageContent
│       │   ├── file.py          # BlobFileInfo, FileTreeNode
│       │   └── skill.py         # SkillSummary, SkillDetail
│       ├── routers/
│       │   ├── chat.py          # POST /api/chat, WS /api/chat/stream
│       │   ├── agents.py        # CRUD for .agent.md files
│       │   ├── skills.py        # CRUD for .skill.md files
│       │   ├── files.py         # Upload, download, browse blob storage
│       │   ├── logs.py          # Log snapshot + WS streaming
│       │   └── telegram.py      # Webhook receive/setup
│       └── services/
│           ├── copilot.py       # Runs Copilot CLI, streams output, manages logs
│           ├── session_manager.py  # Chat session persistence (blob-backed)
│           ├── response_parser.py  # Parses CLI output into structured blocks
│           ├── blob_storage.py     # Azure Blob / local file abstraction
│           ├── agent_parser.py     # Reads/writes .agent.md and .skill.md
│           └── telegram_bot.py     # Telegram message handling + history
├── frontend/
│   ├── index.html               # Two-panel dashboard (file explorer + logs)
│   ├── css/style.css            # Dark theme, VS Code-inspired
│   └── js/app.js               # File tree, log streaming, preview modal
├── workspace/                   # Mounted volume for .agent.md / .skill.md files
├── Dockerfile                   # Python 3.12 + standalone copilot CLI
├── deploy.sh                    # Provisions Azure resources, builds & deploys
├── update.sh                    # Rebuilds image and restarts container
└── setup-telegram.sh            # Interactive Telegram bot setup
```

## Core Flows

### Chat (WebSocket Streaming)

```
Client                    Backend                    Copilot CLI
  │                          │                           │
  │── WS connect ──────────▶│                           │
  │── {message, agent} ────▶│                           │
  │                          │── create/load session ──▶│ (blob storage)
  │                          │── spawn subprocess ─────▶│
  │                          │                           │── query AI model
  │                          │◀── stdout chunks ────────│
  │◀── {type:"chunk"} ──────│                           │
  │◀── {type:"tool"} ───────│  (tool call detected)    │
  │◀── {type:"chunk"} ──────│                           │
  │                          │◀── process exits ────────│
  │                          │── parse response ───────▶│
  │                          │── sync workspace files ─▶│ (blob storage)
  │                          │── save session ─────────▶│ (blob storage)
  │◀── {type:"done"} ───────│                           │
```

1. Client opens WebSocket to `/api/chat/stream` and sends JSON with message, optional agent name, and session ID.
2. Backend loads or creates a `ChatSession`, saves the user message.
3. The backend runs `copilot --output-format json --allow-all --agent <agent-name> -p <prompt>`. If an agent is specified, it's passed via `--agent`; otherwise no agent flag is set (freeform chat).
4. The CLI outputs structured JSONL events. The backend parses `assistant.message_delta` (text chunks), `tool.execution_start` (tool calls), `assistant.turn_start` (turn boundaries), `session.error` (errors), and `result` (completion). Tool events are forwarded to the client as separate messages.
5. On completion, the raw output is parsed into structured `MessageContent` blocks (text, code, file references). Workspace files are synced to blob storage. The assistant message is saved to the session.

### Synchronous Chat

`POST /api/chat` — Same flow but waits for the full response before returning. Used for short queries.

### Telegram Bot

```
Telegram ──webhook──▶ /api/telegram/webhook ──▶ telegram_bot.py
                                                    │
                                                    ├── parse command (/agent, /plan, etc.)
                                                    ├── call copilot service
                                                    ├── stream + edit message in chat
                                                    └── store history (10 turns, 4000 chars)
```

Commands like `/agent <name> <prompt>` trigger the same Copilot service. Responses are streamed by editing the Telegram message progressively. Per-chat history (in-memory, last 10 turns) provides conversation context.

## Copilot CLI

All AI interactions go through the standalone `copilot` CLI (`@github/copilot` npm package) in non-interactive mode with structured JSONL output:

```
copilot --output-format json --allow-all [--agent <agent>] [--model <model>] -p <prompt>
```

| Flag | Purpose |
|------|---------|
| `--output-format json` | JSONL event stream (deltas, tool calls, errors) |
| `--allow-all` | Enable all permissions (tools, paths, URLs) |
| `--agent <agent>` | Use a custom agent (resolved by name from `.agent.md` files) |
| `--model <model>` | Override AI model (e.g., `claude-opus-4.6-1m`) |
| `--available-tools` | Restrict tools (used in plan mode for read-only: `read`, `search`, `web`) |
| `-p <prompt>` | Non-interactive prompt (exits after completion) |

The `copilot` CLI discovers agents from `.github/agents/*.agent.md` files relative to the **git root** of the working directory. The workspace directory is initialized as a git repo at startup (via `ensure_workspace_dirs()` and in the Dockerfile) so agent discovery works correctly.

The backend reads JSONL events from stdout and dispatches them:

| Event type | Action |
|-----------|--------|
| `assistant.message_delta` | Yield text chunk to client |
| `tool.execution_start` | Emit tool event marker (`\x00TOOL:name\|desc`) |
| `assistant.turn_start` | Insert turn separator |
| `session.error` | Yield error, kill process |
| `result` | Process complete |

## Agents & Skills

**Agents** (`.agent.md` in `workspace/.agents/`) define specialized AI personas:

```yaml
---
name: stock-analysis
description: Analyze stock prices
argument-hint: TICKER at $PRICE
tools: [edit, search, web]
skills: [data-analysis]
---

You are a stock analyst. Given a ticker and price...
```

**Skills** (`.skill.md` in `workspace/.skills/`) are reusable instruction modules that agents reference via the `skills` field. Both are managed through CRUD API endpoints and parsed from YAML frontmatter + markdown body.

## Storage

`blob_storage.py` provides a unified interface with two backends:

- **Azure Blob Storage** (production): Uses `AZURE_STORAGE_CONNECTION_STRING`. Files stored in the `workspace` container.
- **Local filesystem** (development): Falls back when no connection string is set. Files stored under `WORKSPACE_DIR`.

Used for: user-uploaded files, Copilot-generated files, chat session JSON (`sessions/{uuid}.json`), agent/skill markdown files.

## Authentication

Controlled by `AUTH_ENABLED` env var:

- **Disabled** (default, local dev): All requests get a dummy user identity.
- **Enabled** (production): Validates Azure Entra ID JWT tokens (RS256). Checks audience, issuer, and expiration. JWKS keys are fetched and cached from Microsoft's discovery endpoint.

## Real-Time Logs

The Copilot service maintains an in-memory activity log (last 500 entries) with async subscriber queues. The frontend connects via WebSocket to `/api/logs/stream` and receives:

- **Snapshot**: Current log buffer + any active process
- **Text deltas**: Incremental CLI output
- **Tool events**: Tool name + description
- **Process lifecycle**: Start/end markers with status

## Deployment

`deploy.sh` provisions the full Azure stack:

1. Resource Group
2. Azure Container Registry — image built in the cloud (no local Docker needed)
3. Storage Account — for blob storage
4. Container Apps Environment + Container App — runs the FastAPI server on port 8000, scales 0–1 replicas
5. Azure Function App — timer trigger for cron job execution (Consumption plan, runs every 1 min)

`update.sh` rebuilds the image in ACR and restarts the container app.

The **Dockerfile** installs Python 3.12, the standalone GitHub Copilot CLI (`@github/copilot` via npm), and mounts `/workspace` for agent/skill files.

## Scheduled Jobs (Cron)

Users can schedule recurring agent runs via the Telegram `/cron` command. Jobs are persisted in Azure Blob Storage and executed by an external Azure Function timer.

```
Telegram                    Container App                 Azure Function
   │                        (scale-to-zero)               (timer, every 1 min)
   │── /cron daily ──▶      │                              │
   │                         │── save job to blob ──▶      │
   │◀── "Job created" ──    │                              │
   │                         │     (sleeping)               │
   │                         │                              │── GET /api/cron/due
   │                         │◀─── wakes up ───────────────│
   │                         │── return due job IDs ──────▶│
   │                         │                              │── POST /api/cron/run/{id}
   │                         │◀─── wakes up ───────────────│
   │                         │── run agent ──▶              │
   │                         │── send email ──▶             │
   │◀── "✅ completed" ──   │                              │
```

**Components:**
- **`cron_store.py`** — CRUD for jobs, persisted as `cron/jobs.json` in blob storage. Each job has an ID, agent, prompt, schedule preset, email, and `last_run` timestamp. `is_job_due()` compares `last_run + interval` against current time.
- **`/api/cron/due`** — Returns IDs of jobs whose interval has elapsed. Secured with `X-Cron-Secret` header.
- **`/api/cron/run/{id}`** — Executes a job: runs the agent via `copilot` CLI, emails full results via Azure Communication Services, sends a short Telegram notification (success/failure). Secured with `X-Cron-Secret`.
- **Azure Function** (`azure-function/`) — Timer trigger that fires every minute, calls `/api/cron/due`, then `/api/cron/run/{id}` for each due job. Wakes the container only when work is needed.

**Schedule presets:** `every 1h`, `every 6h`, `daily`, `weekly`, `weekdays`

**Telegram commands:**
- `/cron <schedule> <agent> <prompt> --email user@example.com` — create a job
- `/crons` — list your jobs (shows IDs)
- `/uncron <id>` — delete a job

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `GH_TOKEN` | GitHub CLI authentication |
| `AZURE_STORAGE_CONNECTION_STRING` | Blob storage access |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_WEBHOOK_SECRET` | Webhook verification secret |
| `WORKSPACE_DIR` | Agent/skill file location (default: `/workspace`) |
| `AUTH_ENABLED` | Toggle Azure AD auth (`true`/`false`) |
| `COPILOT_MODEL` | Default AI model |
| `CORS_ORIGINS` | Allowed CORS origins (default: `["*"]`) |
| `AZURE_COMM_CONNECTION_STRING` | Azure Communication Services (for cron email) |
| `EMAIL_SENDER_ADDRESS` | Sender email address for cron notifications |
| `CRON_SECRET` | Shared secret for cron API auth (auto-generated by deploy.sh) |
