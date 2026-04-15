# OpenCopilot - Deployment, Cost, and Trade-Offs

## One-Command Deployment

The entire OpenCopilot Azure infrastructure - container registry, blob storage, container app, function app, email service - is provisioned by a single script with zero prerequisites beyond the Azure CLI and a GitHub token.

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

## The Real Cost

Here's the exact breakdown for a personal or small-team deployment:

| Resource | Pricing Model | Typical Monthly Cost |
|----------|--------------|---------------------|
| Azure Container Apps | Pay per vCPU-second + memory-second, **scale to zero** | **$0 – $3** (idle most of the time) |
| Azure Container Registry | Basic tier | **$5/month** |
| Azure Blob Storage | Standard LRS, pay per GB + operations | **< $1** (reports are tiny text files) |
| Azure Functions | Consumption plan, free grant: 1M executions/month | **$0** (runs every 5 min = ~8,640/month) |
| Azure Communication Services | Email: $0.00025/email | **< $0.01** |
| GitHub Copilot | Included with your existing subscription | **$0 incremental** |

**Total: ~$5-8/month** for a system that's mostly idle. Compare that to running Claude Code agents via the API - a single 10-module stock analysis (with 30+ web searches and extensive reasoning). With Copilot, that same analysis costs $0 incremental because it's included in the subscription you're already paying for.

The key enabler is **scale-to-zero** on Azure Container Apps:

```yaml
min-replicas: 0
max-replicas: 1
cooldownPeriod: 1800  # 30 minutes
```

When nobody is using the bot, no containers run. When a Telegram message or API request arrives, cold start takes 5-10 seconds, then the container stays warm for 30 minutes. The longer cooldown ensures that multi-turn conversations and long-running agent tasks (a 10-module stock analysis can take 3-5 minutes) don't get killed by a premature scale-down.

## The Cron Scheduling System

You can schedule any agent to run on a recurring basis and email results automatically.

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

So if your daily stock analysis generates 11 files (10 modules + final synthesis), you get all of them in a single email every morning.

## The Web Dashboard

The web frontend is intentionally minimal - two panels:

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

The dashboard serves as a monitoring and file management layer. Chat happens through Telegram or the API - the dashboard shows you what's happening inside. File previews use Marked.js for markdown and highlight.js for code, so generated reports render with tables, headers, and syntax highlighting.

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

## Security

OpenCopilot runs with `--allow-all` on the Copilot CLI, which means the AI agent has unrestricted access to:
- Shell commands
- File reads and writes
- Web browsing
- Sub-agent invocation

This is by design - agents need to write reports, run Python scripts, fetch web data, and invoke other agents. The answer isn't to limit the AI - it's to limit who can talk to the AI. You should never expose this to untrusted users.

Here's how access is controlled:

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

## Limitations

Here are the real trade-offs:

### Cold Start Latency

With scale-to-zero, the first request after idle takes 5-10 seconds while the container starts. In practice, this is barely noticeable for Telegram - you send a message and get a typing indicator within a few seconds. For latency-sensitive API use cases, you can set `min-replicas: 1` at the cost of ~$15-30/month.

### Single Replica

The app runs with `max-replicas: 1`. The Copilot CLI process and workspace directory aren't designed for concurrent access from multiple containers. Multiple Telegram users can send messages concurrently, but they're queued per-chat with `asyncio.Lock` to avoid workspace conflicts.

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

The entire system depends on the `copilot` CLI binary staying backwards-compatible. If GitHub changes the JSONL output format, renames the `--agent` flag, or deprecates the CLI altogether, everything breaks. The parser is built defensively (unknown event types are silently skipped), but this is inherent vendor lock-in to a CLI binary. The CLI is actively maintained and growing, but it's a risk worth acknowledging.

### No GPU, No Local Models

All AI inference happens through GitHub's hosted models via the CLI. You can't run local models or fine-tuned models. You're limited to what GitHub Copilot exposes (currently Claude, GPT, and Gemini families).

### 5-Minute Timeout

The Copilot process has a 300-second (5-minute) timeout. If an agent runs longer - say, a portfolio analysis screening 20 tickers - it gets killed. This protects against runaway processes but limits very large analyses.

## Conclusion

The platform side of OpenCopilot is intentionally boring. A deployment script, a container, some blob storage, a timer function. None of it is novel. But that's the point - the infrastructure should disappear so the agents can do their thing.

The part I'm most proud of is the cron system. "Run this agent every morning and email me the results" is a single Telegram command, and it changed how I use AI entirely. It went from something I interact with to something that works for me in the background. That shift - from tool to assistant - is what made the whole project click.

Every design choice here optimizes for one thing: keeping it cheap and simple enough that I actually use it every day. So far, that's held up.

**GitHub: [github.com/arjunpatel17/OpenCopilot](https://github.com/arjunpatel17/OpenCopilot)**
