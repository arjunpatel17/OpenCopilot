# Architecture

## Overview

OpenCopilot is a cloud-native application that lets users run GitHub Copilot agents via a **web dashboard**, **REST/WebSocket API**, or **Telegram bot**. It wraps the GitHub Copilot CLI (`gh copilot` / `code chat`) with session management, file storage, real-time streaming, and a custom agent/skill system — all deployed on Azure.

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
│  │ gh copilot CLI   │   │ In-Memory Log   │                     │
│  │ / code chat CLI  │   │ Buffer + Queues │                     │
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
| AI | GitHub Copilot CLI — **`code chat`** (preferred, VS Code CLI) with **`gh copilot`** fallback |
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
├── Dockerfile                   # Python 3.12 + gh CLI + Copilot extensions
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
3. If an agent is specified, the backend uses a **two-tier CLI strategy**:
   - **Primary — `code chat`**: If the VS Code CLI (`code`) is on `PATH`, runs `code chat -m <agent-name> <prompt>`. This is the native agent runner — it resolves the agent by name and handles tool execution internally.
   - **Fallback — `gh copilot`**: If `code` is not available, loads the agent's `.agent.md` instructions, prepends them to the prompt, and sends everything through `gh copilot --allow-all --output-format json`. This is a manual simulation of agent behavior.
   - Without an agent, `gh copilot` is used directly for freeform chat.
4. CLI output streams back in 100ms chunks. Tool-call markers (`\x00TOOL:`) are detected and sent as separate events.
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

## Copilot CLI Strategy

The backend uses two different CLI tools to talk to GitHub Copilot, with automatic fallback:

```
run_code_chat(prompt, agent_name)
        │
        ├── `code` binary found?
        │       │
        │      YES ──▶ code chat -m <agent-name> <prompt>
        │               (VS Code CLI — native agent runner, handles
        │                tool execution and agent resolution internally)
        │
        └──── NO ──▶ run_with_agent(prompt, agent_name)
                        │
                        ├── Load .agent.md instructions from workspace
                        ├── Prepend instructions to prompt
                        └── gh copilot --allow-all --output-format json
                            (or standalone `copilot` binary)
```

| CLI | When used | How agents work |
|-----|-----------|-----------------|
| **`code chat`** | Preferred — when `code` is on `PATH` (e.g., in the Docker container with VS Code CLI installed) | Agent resolved by name via `-m <agent>`. Tool calls and instructions handled natively. |
| **`gh copilot`** | Fallback — when `code` is not available | Agent `.agent.md` body loaded from disk, prepended to the user prompt manually. Tools managed via `--allow-all` flag. |

Both CLIs stream stdout, which the backend reads in 100ms chunks. Tool-call markers (`\x00TOOL:name:description`) are detected inline and forwarded as separate events to the client.

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

`update.sh` rebuilds the image in ACR and restarts the container app.

The **Dockerfile** installs Python 3.12, the GitHub CLI with Copilot extensions, and mounts `/workspace` for agent/skill files.

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
