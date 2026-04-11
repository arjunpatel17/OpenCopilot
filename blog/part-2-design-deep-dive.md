# Part 2: OpenCopilot — $5/Month, One-Command Deploy, and Everything That Can Go Wrong

## One-Command Deployment (No, Really)

I have a pet peeve with "easy deployment" claims in the developer community. You know the ones: "just run this script!" and then the README has 47 prerequisites, three manual configuration steps, and a "troubleshooting" section longer than the actual guide.

So let me be clear: **the entire OpenCopilot Azure infrastructure — container registry, blob storage, container app, function app, email service — is provisioned by a single script with zero prerequisites beyond the Azure CLI and a GitHub token.**

```bash
./deploy.sh
```

No Docker Desktop required. No manual resource creation. No YAML files to stare at. The script runs 10 steps and takes about 5-7 minutes:

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

At the end, it prints your public URL. If you've ever spent a weekend wrestling with Kubernetes manifests or CloudFormation templates, this will feel almost suspiciously simple:

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

## The Real Cost (I'm Tired of Vague "Serverless Is Cheap" Claims)

Every serverless article says "it's basically free!" and then hand-waves past the actual numbers. Let me give you the exact breakdown for a personal or small-team deployment — no asterisks, no "it depends":

| Resource | Pricing Model | Typical Monthly Cost |
|----------|--------------|---------------------|
| Azure Container Apps | Pay per vCPU-second + memory-second, **scale to zero** | **$0 – $3** (idle most of the time) |
| Azure Container Registry | Basic tier | **$5/month** |
| Azure Blob Storage | Standard LRS, pay per GB + operations | **< $1** (reports are tiny text files) |
| Azure Functions | Consumption plan, free grant: 1M executions/month | **$0** (runs every 5 min = ~8,640/month) |
| Azure Communication Services | Email: $0.00025/email | **< $0.01** |
| GitHub Copilot | Included with your existing subscription | **$0 incremental** |

**Total: ~$5-8/month** for a system that's mostly idle. Compare that to running Claude Code agents via the API — a single 10-module stock analysis (with 30+ web searches and extensive reasoning) would cost $3-8 in API tokens. *Per run.* With Copilot, that same analysis costs $0 incremental because it's included in the subscription you're already paying for. The math here isn't even close.

If you drop ACR to a slightly older image and use managed builds less frequently, you can push this even lower.

The key enabler is **scale-to-zero** on Azure Container Apps. I configured the app with:

```yaml
min-replicas: 0
max-replicas: 1
cooldownPeriod: 1800  # 30 minutes
```

When nobody is using the bot, no containers run. When a Telegram message or API request arrives, cold start takes 5-10 seconds, then the container stays warm for 30 minutes. The longer cooldown ensures that multi-turn conversations and long-running agent tasks (a 10-module stock analysis can take 3-5 minutes) don't get killed by a premature scale-down.

## The Cron Scheduling System (My Proudest Feature)

Here's where OpenCopilot stops being a "cool hack" and starts being *actually useful*. You can schedule any agent to run on a recurring basis and email results automatically.

Ask yourself: what would you automate if you could schedule any AI agent to run on any cadence, with zero marginal cost?

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

So if your daily stock analysis generates 11 files (10 modules + final synthesis), you get all of them in a single email. Every morning in your inbox. Before your first coffee. Is it overkill? Maybe. But have you ever gotten institutional-grade equity research delivered to your inbox *for free* while you were sleeping? It hits different.

## The Web Dashboard (Minimalism as a Feature, Not Laziness)

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

### Key UI Decisions (Where I'll Get Controversial)

**VS Code dark theme** — if you're deploying a developer tool, it should look like a developer tool. I've seen too many AI dashboards with bubbly pastel UIs that scream "we hired a designer but not an engineer." The CSS uses the same color palette as VS Code's dark theme. It's opinionated. I like it.

**No chat in the UI** — this is the decision that gets the most pushback, and I stand by it 100%. I considered adding a chat panel. Then I realized: Telegram already handles chat *perfectly* — message history, mobile push notifications, voice input, threading, image sharing. Duplicating it in the web UI means maintaining two chat implementations, neither of which would be as good as Telegram. The web dashboard does what Telegram *can't* do: file browsing and log monitoring. Each tool plays to its strength. Stop trying to make every UI do everything.

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

## Security: The "--allow-all" Elephant in the Room

Let's address this head-on because it makes security engineers twitch. OpenCopilot runs with `--allow-all` on the Copilot CLI, which means the AI agent has unrestricted access to:
- Shell commands
- File reads and writes
- Web browsing
- Sub-agent invocation

This is by design — agents need to write reports, run Python scripts, fetch web data, and invoke other agents. If you restrict tool access, you neuter the agents. **The answer isn't to limit the AI. The answer is to limit who can talk to the AI.** You should **never expose this to untrusted users.**

Here's how I layered the defenses (and yes, I've thought about this more than you might expect):

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

## The Honest Limitations Section (Read This Before You Deploy)

I could end the post here and let you think everything is roses. But I'd rather you know what you're getting into. Here are the real trade-offs, ranked by how much they've actually bitten me:

### Cold Start Latency (Annoyance Level: Low)

With scale-to-zero, the first request after idle takes 5-10 seconds while the container starts. In practice? This is barely noticeable for Telegram — you send a message and get a typing indicator within a few seconds, which feels natural. For latency-sensitive programmatic API use cases, you might want `min-replicas: 1` at the cost of ~$15-30/month. But honestly, if you're building a latency-sensitive app on top of an AI agent that takes 2-5 minutes to complete, your 5-second cold start is not the bottleneck.

### Single Replica (Annoyance Level: Medium)

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

### CLI Dependency (Annoyance Level: High — My Biggest Worry)

This is the one that keeps me up at night. The entire system depends on the `copilot` CLI binary staying backwards-compatible. If GitHub changes the JSONL output format, renames the `--agent` flag, or deprecates the CLI altogether, everything breaks. I've built the parser defensively (unknown event types are silently skipped), but let's be honest — this is inherent vendor lock-in, just to a CLI binary instead of a cloud API. If GitHub decides to kill the standalone CLI, I'm rewriting the core of this project from scratch.

Is it likely? Probably not — the CLI is actively maintained and growing. But it's a risk I accepted with eyes wide open.

### No GPU, No Local Models (Annoyance Level: Depends on Who You Ask)

All AI inference happens through GitHub's hosted models via the CLI. You can't run local models or fine-tuned models. You're limited to what GitHub Copilot exposes (currently Claude, GPT, and Gemini families). For some people, this is a dealbreaker. For me? These are the best models in the world. I'm not trying to run a fine-tuned 7B model for stock analysis — I want Claude and GPT-4o, and I get them for free.

### 5-Minute Timeout (Annoyance Level: Occasional)

The Copilot process has a 300-second (5-minute) timeout. If an agent runs longer — say, a portfolio analysis screening 20 tickers — it gets killed. This protects against runaway processes but limits very large analyses.

## What Actually Makes This Worth Building

After months of using OpenCopilot daily, the value isn't "AI in the cloud" — you can get that from a dozen products. The value is the *combination* of things that no single product offers:

1. **The full agent runtime runs in the cloud.** Not just a model. Not just a chatbot. The entire orchestration system — agents invoking sub-agents, skills composing into workflows, web research feeding into file generation — running headlessly in a container. Try building that from scratch with raw API calls. I'll wait.

2. **Telegram as the interface.** No app to build. No web UI to maintain. Send a message, get a report. Send a voice memo, get an analysis. Attach an image, get feedback. It sounds simple because it is. That's the point.

3. **Cron scheduling with email.** "Run this agent every morning and email me the results." That sentence is worth the entire project. Zero ongoing interaction. Zero daily effort. Just results in your inbox.

4. **File persistence.** Agents generate files. Those files survive container restarts. They're browsable in a web dashboard. It sounds boring but it's the difference between a toy and a tool.

5. **It costs almost nothing.** I'll say it again: ~$5/month. That's less than a single GPT-4 API-powered analysis session. Stop overthinking the cost.

## Coming Up: The Agents That Make This Worth It

Parts 1 and 2 covered the platform. Parts 3 and 4 cover the *payoff* — the agents that actually run on this thing and deliver real value every day.

In Part 3, I'll walk through the **Portfolio Advisor Agent** — an agent that screens your stock portfolio against 7-day moving averages, flags significant movers, runs deep 10-module institutional-grade analysis on each flagged ticker, and delivers a consolidated buy/sell/hold report. It's the agent I run every single morning.

In Part 4, it's the **Real Estate Analysis Agent** — 6-module property investment analysis covering valuation, neighborhood intelligence, cash flow, market trends, financing, and risk. Point it at a Zillow listing, get an investor-ready verdict.

If Part 1 made you think "why would anyone wrap a CLI?", these next posts will answer that.

---

*This is Part 2 of a 4-part series on OpenCopilot. [← Part 1: Architecture & Vision](part-1-architecture-and-vision.md) | [Part 3: Portfolio Advisor Agent →](part-3-portfolio-advisor-agent.md)*

**GitHub: [github.com/arjunpatel17/OpenCopilot](https://github.com/arjunpatel17/OpenCopilot)**
