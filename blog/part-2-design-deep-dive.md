# Part 2: OpenCopilot — Deployment, Cost, Cron Jobs, and the Trade-Offs

## One-Command Deployment

The entire Azure infrastructure is provisioned by a single script:

```bash
./deploy.sh
```

No Docker Desktop required. No manual resource creation. The script runs 10 steps and takes about 5-7 minutes:

| Step | What It Creates |
|------|----------------|
| 1 | **Resource Group** — `opencopilot-rg` in East US |
| 2 | **Azure Container Registry (ACR)** — stores your Docker image |
| 3 | **Docker Image** — built in the cloud via ACR Tasks (no local Docker) |
| 4 | **Storage Account** — Azure Blob Storage for files, sessions, cron jobs |
| 5 | **Container Apps Environment** — the serverless hosting platform |
| 6-7 | **Container App** — deployed with secrets, ingress, scale-to-zero |
| 8 | **Azure Function** — timer trigger for cron job execution |
| 9 | **Azure Communication Services** — email delivery for scheduled reports |
| 10 | **Email configuration** — links the email domain to the container app |

At the end, it prints your public URL:

```
============================================
  DEPLOYMENT COMPLETE!
============================================

  App URL:  https://opencopilot.abc123.eastus.azurecontainerapps.io

  Resource Group:     opencopilot-rg
  Container Registry: opencopilotacr7f3a2b
  Storage Account:    opencopilotsa4e8c1d
  Function App:       opencopilot-cron
  Email Sender:       DoNotReply@abc123.azurecomm.net
```

### How Authentication Flows Through Deployment

The deployment script captures your GitHub CLI token (`gh auth token`) and injects it as a secret into the container app. Inside the container, the `GH_TOKEN` environment variable is all the `copilot` CLI needs to authenticate with GitHub's AI models. No interactive login required.

Azure secrets are used for sensitive values — `GH_TOKEN`, storage connection strings, cron secrets, and email credentials are never stored as plain-text environment variables.

### Updating After Code Changes

Once deployed, updating is a two-command operation (or use `./update.sh`):

```bash
az acr build --registry YOUR_ACR --image opencopilot:latest --file Dockerfile .
az containerapp update --resource-group opencopilot-rg --name opencopilot \
  --image YOUR_ACR.azurecr.io/opencopilot:latest
```

The image is rebuilt in the cloud and the container app pulls the new version.

## Cost Breakdown

This is the part most people ask about. Here's the real cost for a personal or small-team deployment:

| Resource | Pricing Model | Typical Monthly Cost |
|----------|--------------|---------------------|
| Azure Container Apps | Pay per vCPU-second + memory-second, **scale to zero** | **$0 – $3** (idle most of the time) |
| Azure Container Registry | Basic tier | **$5/month** |
| Azure Blob Storage | Standard LRS, pay per GB + operations | **< $1** (reports are tiny text files) |
| Azure Functions | Consumption plan, free grant: 1M executions/month | **$0** (runs every 5 min = ~8,640/month) |
| Azure Communication Services | Email: $0.00025/email | **< $0.01** |
| GitHub Copilot | Included with your existing subscription | **$0 incremental** |

**Total: ~$5-8/month** for a system that's mostly idle. If you drop ACR to a slightly older image and use managed builds less frequently, you can push this even lower.

The key enabler is **scale-to-zero** on Azure Container Apps. I configured the app with:

```yaml
min-replicas: 0
max-replicas: 1
cooldownPeriod: 1800  # 30 minutes
```

When nobody is using the bot, no containers run. When a Telegram message or API request arrives, cold start takes 5-10 seconds, then the container stays warm for 30 minutes. The longer cooldown ensures that multi-turn conversations and long-running agent tasks (a 10-module stock analysis can take 3-5 minutes) don't get killed by a premature scale-down.

## The Cron Scheduling System

One of the features I'm most proud of is the cron system. You can schedule any agent to run on a recurring basis and email results automatically.

### How It Works

```
┌──────────────────┐     every 5 min     ┌──────────────────┐
│  Azure Function  │ ──────────────────► │  Container App   │
│  (timer trigger) │   GET /api/cron/due │  FastAPI backend  │
│                  │ ◄────────────────── │                  │
│                  │   {due: ["abc123"]} │                  │
│                  │                     │                  │
│                  │  POST /api/cron/run │                  │
│                  │  ─────────────────► │ → runs agent     │
│                  │                     │ → emails results │
│                  │                     │ → notifies TG    │
└──────────────────┘                     └──────────────────┘
```

1. An Azure Function runs every 5 minutes on a consumption plan
2. It calls `GET /api/cron/due` on your container app (authenticated via a shared secret)
3. The container app checks which jobs are due based on their schedule and last run time
4. For each due job, the function calls `POST /api/cron/run/{job_id}`
5. The container runs the agent, collects the full output + generated files, emails everything, and sends a short Telegram notification

### Creating a Cron Job from Telegram

```
/cron daily stock-analysis-pro AAPL at $198.50 --email me@company.com --time 08:00
```

This creates a job that:
- Runs the `stock-analysis-pro` agent every day at 08:00 UTC
- Passes the prompt "AAPL at $198.50"
- Emails the full report (including all 10 generated analysis files) to `me@company.com`
- Sends a ✅ or ❌ notification to your Telegram chat

Schedule presets: `every 1h`, `every 6h`, `daily`, `weekly`, `weekdays`

### Persistence

Cron jobs are stored as JSON in Azure Blob Storage (`cron/jobs.json`). This means they survive container restarts and redeployments. The Azure Function is stateless — it just checks what's due and triggers execution.

### The Email

When a job runs, the email contains:
- The full text output from the agent
- All generated files embedded inline (the complete markdown content of every report)
- Error details if something failed

So if your daily stock analysis generates 11 files (10 modules + final synthesis), you get all of them in a single email.

## The Web Dashboard

The web frontend is intentionally minimal — two panels:

**Left panel: File Explorer**
- Browse all files in the workspace (agents, skills, reports, uploaded files)
- Click to preview any file with syntax highlighting
- Upload files (drag and drop)
- Download individual files or entire folders as ZIP
- Delete files and folders
- Auto-refreshes when new files appear

**Right panel: Process Logs**
- Real-time WebSocket stream of all Copilot activity
- Shows tool calls, text output, process start/end events
- Status badge shows whether a process is running or idle
- Useful for debugging agent behavior or watching a long-running analysis in progress

The dashboard serves as a monitoring and file management layer. Chat happens through Telegram or the API — the dashboard shows you what's happening inside.

### Key UI Decisions

**VS Code dark theme** — if you're deploying a developer tool, it should look like a developer tool. The CSS uses the same color palette as VS Code's dark theme.

**No chat in the UI** — I considered adding a chat panel but decided against it. Telegram already handles chat perfectly (message history, mobile push notifications, voice input, threading). Duplicating it in the web UI would mean maintaining two chat implementations. The web dashboard focuses on what Telegram can't do: file browsing and log monitoring.

**Markdown rendering with syntax highlighting** — file previews use Marked.js for markdown and highlight.js for code. When you click on a generated stock analysis report, you see it fully rendered with tables, headers, and code blocks.

## Storage Architecture

The `blob_storage.py` module provides a unified interface with two backends:

```python
# If Azure connection string is set → use Azure Blob Storage
# Otherwise → use local filesystem
_use_azure = bool(settings.azure_storage_connection_string)
```

This means:
- **Local development**: files live in the `workspace/` directory on your machine. No Azure account needed.
- **Production**: files are persisted in Azure Blob Storage. Container restarts don't lose data.

The local backend includes path traversal protection:

```python
def _local_path(blob_path: str) -> Path:
    clean = Path(blob_path)
    if clean.is_absolute() or ".." in clean.parts:
        raise ValueError("Invalid blob path")
    return _local_root() / clean
```

Everything passes through this interface: agent/skill files, generated reports, chat sessions, cron job data, uploaded user files.

## Security Considerations

OpenCopilot runs with `--allow-all` on the Copilot CLI, which means the AI agent has unrestricted access to:
- Shell commands
- File reads and writes
- Web browsing
- Sub-agent invocation

This is by design — agents need to write reports, run Python scripts, fetch web data, and invoke other agents. But it means you should **never expose this to untrusted users.**

The security layers:

| Layer | Protection |
|-------|-----------|
| **Azure Entra ID** | JWT authentication on all API endpoints (optional, disabled for local dev) |
| **Telegram allowlist** | Only specified usernames can use the bot (`TELEGRAM_ALLOWED_USERS`) |
| **Cron secret** | HMAC-verified shared secret between the Azure Function and container app |
| **Non-root container** | The Docker container runs as `appuser`, not root |
| **Security headers** | `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` on all responses |
| **CORS** | Configurable allowed origins (defaults to localhost) |
| **Webhook secret** | Telegram webhook verified via secret token |

The auth system supports both REST and WebSocket:
- REST: Standard `Authorization: Bearer <token>` header
- WebSocket: Token passed as query parameter (`?token=<jwt>`)
- JWKS keys are cached for 6 hours and refreshed automatically

## Limitations and Trade-Offs

### Cold Start Latency

With scale-to-zero, the first request after idle takes 5-10 seconds while the container starts. For Telegram, this is acceptable — you send a message and get a typing indicator within a few seconds. For latency-sensitive API use cases, you might want `min-replicas: 1` at the cost of ~$15-30/month.

### Single Replica

The app runs with `max-replicas: 1`. This is intentional — the Copilot CLI process and workspace directory aren't designed for concurrent access from multiple containers. Multiple Telegram users can send messages concurrently, but they're queued per-chat with `asyncio.Lock` to avoid workspace conflicts.

### In-Memory State

Some state is in-memory and lost on restart:
- Conversation history in Telegram (last 10 turns per chat)
- Process activity logs
- Chat lock state

This is acceptable because:
- Telegram history is a convenience feature, not critical data
- Process logs are ephemeral by nature
- Chat sessions (the structured `ChatSession` objects) are persisted in Blob Storage

### CLI Dependency

The entire system depends on the `copilot` CLI binary staying backwards-compatible. If GitHub changes the JSONL output format or drops the `--agent` flag, things break. I've built the parser defensively (unknown event types are silently skipped), but this is an inherent risk of wrapping a third-party CLI.

### No GPU, No Local Models

All AI inference happens through GitHub's hosted models via the CLI. You don't need GPUs, but you can't run local models or fine-tuned models. You're limited to what GitHub Copilot exposes (which is currently Claude, GPT, and Gemini families).

### 5-Minute Timeout

The Copilot process has a 300-second (5-minute) timeout. If an agent runs longer — say, a portfolio analysis screening 20 tickers — it gets killed. This protects against runaway processes but limits very large analyses.

## What Makes This Different From Just Using the API

The value of OpenCopilot isn't "AI in the cloud" — you can get that anywhere. The value is:

1. **The agent and skill system runs in the cloud.** Writing a stock analysis agent with 10 modular skills, each performing live web research and writing structured reports, is something the CLI agent runtime handles beautifully. Replicating that with raw API calls would mean building your own tool orchestration system.

2. **Telegram as the interface.** No app to build. No web UI to maintain. Send a message, get a report. Send a voice memo, get an analysis. Attach an image, get feedback.

3. **Cron scheduling with email.** "Run this agent every morning and email me the results" is a workflow that requires zero ongoing interaction. Set it and forget it.

4. **File persistence.** Agents generate files. Those files need to survive container restarts and be browsable. Blob Storage handles this transparently.

5. **It costs almost nothing.** Scale-to-zero means personal use is essentially free beyond the $5/month ACR cost.

## Coming Up

In Part 3, I'll walk through a real example: the **Portfolio Advisor Agent** — an agent that screens a portfolio of stocks against their 7-day moving averages, flags significant movers, runs deep 10-module analysis on each flagged ticker, and delivers a consolidated buy/sell/hold report.

In Part 4, I'll cover the **Real Estate Analysis Agent** — a 6-module property investment analysis that evaluates valuation, neighborhood intelligence, cash flow, market trends, financing, and risk.

---

*This is Part 2 of a 4-part series on OpenCopilot. [← Part 1: Architecture & Vision](part-1-architecture-and-vision.md) | [Part 3: Portfolio Advisor Agent →](part-3-portfolio-advisor-agent.md)*
