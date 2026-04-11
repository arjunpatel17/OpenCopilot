# Part 1: OpenCopilot — I Put GitHub Copilot in the Cloud and Now It Runs My Mornings

## The Problem: The Best AI Agent Runtime Is Trapped on Your Laptop

Here's something that's been bugging me: GitHub Copilot's standalone CLI (`@github/copilot`) is quietly one of the most powerful AI agent runtimes available. Custom agents via `.agent.md` files, web search, shell access, composable skills, structured JSONL output — it's an AI software engineer that actually *does things*, not just *talks about things*.

But it only runs on your local machine. And that's a problem nobody seems to be talking about.

Think about it:
- You can't trigger an agent from your phone while commuting
- You can't schedule a stock analysis to run every morning at 8 AM and email you the report
- Your friends or team can't use your agents without cloning your repo and setting up the CLI
- Long-running agent tasks (10+ minutes of web searches, analysis, file generation) tie up your terminal

Every "AI agent" startup is building agent runtimes from scratch when there's already a great one sitting right there in the npm registry. It just needs a cloud wrapper.

So I built one. **Wrap the Copilot CLI in a cloud service, expose it through a REST API and Telegram bot, add file storage and scheduling, deploy the whole thing to Azure for ~$0/month when idle.**

That's OpenCopilot. And honestly? It's changed how I start my mornings.

## What OpenCopilot Does (And Why You Should Care)

Let me skip the abstract description and show you what this actually looks like. OpenCopilot gives you three ways to talk to your agents:

1. **A web dashboard** — VS Code-inspired dark-themed UI with a file explorer and real-time process logs
2. **REST and WebSocket APIs** — for programmatic access and real-time streaming
3. **A Telegram bot** — chat with your agents from your phone, complete with voice transcription, image analysis, and inline file links

Here's a real interaction:

```
You (Telegram):  /agent stock-analysis-pro AAPL at $198.50
Bot:             ⚡ Web search: AAPL latest earnings...
                 ⚡ Web search: AAPL analyst ratings...
                 ⚡ Writing AAPL-01-goldman-sachs-screener.md
                 ⚡ Writing AAPL-02-morgan-stanley-dcf.md
                 ... (10 analysis modules run in parallel)

                 ═══════════════════════════════════════
                   TICKER: AAPL
                   CURRENT PRICE: $198.50
                   WEIGHTED SCORE: 3.72 / 5.00
                   FINAL GRADE: BUY 🟢
                   12-MONTH TARGET: $235 (+18.4%)
                 ═══════════════════════════════════════

                 📂 11 reports saved. View: [File Explorer link]
```

The agent runs on a container in the cloud. You get the result on your phone. The generated report files are stored in Azure Blob Storage and accessible through the web dashboard.

Pause and let that sink in. You sent a text message. An AI agent spun up in the cloud, performed 20+ web searches, ran 10 analysis modules, wrote 11 files, scored the stock on a weighted multi-factor model, and gave you a buy/sell/hold verdict. From your phone. While you were on the subway.

That's the pitch. Now let me show you how it works.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Clients                                    │
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
│   Routers: /chat, /agents, /skills, /files, /logs, /telegram, /cron │
│                                                                     │
│   Services: Copilot CLI wrapper, Session Manager, Blob Storage,     │
│             Agent Parser, Response Parser, Telegram Bot, Email       │
│                                                                     │
│   ┌──────────────────┐   ┌─────────────────┐                       │
│   │ copilot CLI       │   │ In-Memory Log   │                       │
│   │ (@github/copilot) │   │ Buffer + Queues │                       │
│   └──────────────────┘   └─────────────────┘                       │
└─────────────────────────────────────────────────────────────────────┘
           │                                    │
           ▼                                    ▼
┌─────────────────────┐   ┌─────────────────────┐   ┌────────────────┐
│  GitHub Copilot     │   │  Azure Blob Storage  │   │ Azure Function │
│  (AI models)        │   │  (files, sessions)   │   │ (cron timer)   │
└─────────────────────┘   └─────────────────────┘   └────────────────┘
```

### The Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Backend | Python 3.12, FastAPI, Uvicorn | Async-first, WebSocket support, Pydantic models |
| Frontend | Vanilla HTML/CSS/JS | Zero build step, no framework overhead — the UI is a file explorer, not a SPA |
| AI Engine | `copilot` CLI (`@github/copilot` npm package) | Leverages GitHub's model access, agent system, and tool infrastructure |
| Storage | Azure Blob Storage (prod) / local filesystem (dev) | Persistent file storage that survives container restarts |
| Auth | Azure Entra ID (JWT, RS256) | Enterprise-grade auth, optional for local dev |
| Chat | Telegram Bot API (webhook) | Mobile access, voice messages, image input |
| Deployment | Docker → Azure Container Registry → Azure Container Apps | Serverless scaling, scale-to-zero, ~$0 idle cost |
| Scheduling | Azure Functions (timer trigger) | Serverless cron, checks every 5 minutes |
| Email | Azure Communication Services | Deliver scheduled reports via email |

### Why These Choices? (The Opinionated Part)

**Why Copilot CLI over Claude Code?**

This was the biggest architectural decision, and I know it's a spicy one. Claude Code is excellent — if you forced me to pick one model for raw coding intelligence, I'd probably pick Claude. But *intelligence isn't the bottleneck here*. For building a cloud agent platform, Copilot CLI wins and it's not particularly close:

1. **Model flexibility** — Copilot CLI gives you access to multiple frontier models (Claude, GPT-4o, Gemini, o4-mini) through a single interface. You're not locked into one provider. If Claude Opus is best for deep analysis but GPT-4o is faster for simple lookups, you can pick per-agent or per-request with `--model`. Claude Code only runs Claude.

2. **Cost structure** — If you already have a GitHub Copilot subscription (which most developers do), you get model access included. Claude Code requires a separate Anthropic API key and you pay per token. For a personal tool that runs scheduled jobs daily, those API costs add up. With Copilot, the marginal cost of running an agent is $0.

3. **Agent and skill system** — Copilot's `.agent.md` and `.skill.md` files give you a declarative way to define agents with specific tools, skills, and behaviors. You can compose agents from reusable skill modules, restrict tool access per agent, and manage everything through simple markdown files. Claude Code has custom instructions but nothing as structured or composable.

4. **Built-in tool infrastructure** — Web search, file I/O, shell execution, and sub-agent invocation are all built into the Copilot CLI. With Claude Code, you'd need to wire up MCP servers or custom tool integrations for equivalent functionality.

5. **Structured output** — The `--output-format json` flag gives you a clean JSONL event stream (message deltas, tool calls, errors, results) that's trivial to parse and forward to Telegram or a WebSocket. This made real-time streaming straightforward without custom parsing of terminal escape codes.

6. **GitHub ecosystem integration** — The CLI authenticates via `gh auth token`, which the deployment script already captures. No separate API key management, no token rotation headaches, no billing surprises.

The trade-off is real and I won't sugarcoat it: wrapping a CLI subprocess is *ugly*. You're parsing JSONL from stdout, managing process lifecycles, and yes, I've dealt with zombie processes at 2 AM. It's the kind of architecture that makes backend engineers wince.

But here's my hot take: **elegance is overrated when the alternative costs you money every single day.** The benefits — multi-model access, zero incremental cost, the full agent runtime — made this a no-brainer. I'll take a hacky subprocess wrapper that saves me hundreds in API costs per month over a clean SDK integration that bleeds money.

**"But why not just call the API directly?"**

I get asked this a lot. The answer is simple: **the API gives you a model. The CLI gives you an agent runtime.** The Copilot API endpoints exist, but they don't give you `.agent.md` files, skill composition, tool orchestration, MCP servers, or multi-turn conversations. You'd have to build all of that yourself. By wrapping the CLI, I get the entire agent infrastructure for free — skill discovery, tool invocation, file I/O, web search — orchestrated by a runtime that GitHub's team has already debugged.

**Why FastAPI?**

If you're wrapping a CLI subprocess and streaming its output in real time, you need async. Period. `asyncio.create_subprocess_exec` streams JSONL line by line without blocking, and FastAPI's native WebSocket support pushes those chunks to the browser or Telegram instantly. I tried Flask first — don't. The async gymnastics aren't worth it.

**Why Azure Container Apps instead of AWS Lambda or a VPS?**

Two words: scale to zero. When nobody's using the bot, no containers run. Cost: $0. When a Telegram message arrives, the container spins up in ~5 seconds, processes the request, stays warm for 30 minutes, then disappears again. Lambda can't do this easily because the Copilot CLI needs a persistent filesystem and processes that run for minutes, not milliseconds. A VPS would work but costs $5-20/month whether you use it or not. Container Apps is the sweet spot.

**Why Telegram? Seriously?**

Yes, seriously. Before you judge — ask yourself: when was the last time you built a from-scratch mobile chat UI with message history, typing indicators, push notifications, voice transcription, image sharing, and offline support? That's *months* of work. Telegram gives you all of it for free and it's already on your phone. I send a text, I get a stock analysis. I send a voice memo, it gets transcribed and analyzed. I'm not reinventing the wheel when there's a perfectly good wheel with 800 million users.

**Why vanilla JS? No React? Are you a caveman?**

I can already hear the comments. But hear me out: the frontend is a file explorer and a log viewer. Two panels. No routing. No state management. No component lifecycle. The entire thing is ~400 lines of JavaScript. If I used React, I'd spend more time configuring Vite and arguing about state management libraries than actually building the UI. Sometimes the right tool is no tool. Fight me.

## The Core Flow: How a Text Message Becomes a Wall Street Report

This is the part that still feels like magic to me, even after building it. Let's trace exactly what happens when you send `/agent stock-analysis-pro AAPL at $198.50` from your phone:

### 1. Telegram Webhook → FastAPI

Telegram sends a POST to `/api/telegram/webhook` with the message payload. The router deserializes it into a `telegram.Update` object.

### 2. Command Parsing

The bot handler parses the message:
- Detects `/agent` prefix
- Extracts agent name: `stock-analysis-pro`
- Extracts prompt: `AAPL at $198.50`
- Checks for optional flags: `--model`, `--email`
- Verifies the sender is in the allowed users list

### 3. Conversation History

The bot maintains per-chat history (last 10 turns, max 4,000 characters). If you sent "analyze MSFT" 5 minutes ago, that context gets prepended to the current prompt so the agent knows what you discussed before.

### 4. Copilot CLI Invocation

The `copilot.py` service builds the command:

```bash
copilot --output-format json --allow-all --agent stock-analysis-pro \
  --model claude-opus-4.6-1m -p "AAPL at $198.50"
```

Key flags:
- `--output-format json` — structured JSONL event stream instead of raw text
- `--allow-all` — full tool access (shell, file writes, web)
- `--agent stock-analysis-pro` — tells the CLI to load `stock-analysis-pro.agent.md`
- `-p` — non-interactive mode (exits after completion)

The CLI discovers agents from `.github/agents/*.agent.md` files relative to the git root of the working directory. The workspace is initialized as a git repo at container startup so agent discovery works.

### 5. JSONL Stream Processing

The CLI outputs events like:

```json
{"type": "assistant.message_delta", "data": {"deltaContent": "Analyzing AAPL..."}}
{"type": "tool.execution_start", "data": {"toolName": "web_search", "arguments": {"query": "AAPL earnings Q4 2025"}}}
{"type": "tool.execution_start", "data": {"toolName": "write", "arguments": {"path": "reports/AAPL-01-goldman-sachs-screener.md"}}}
{"type": "assistant.message_delta", "data": {"deltaContent": "Goldman Sachs analysis complete..."}}
{"type": "result", "data": {}}
```

The backend parses each line:
- `assistant.message_delta` → text chunk, forwarded to the client
- `tool.execution_start` → tool event, shown as `⚡ Web search: AAPL earnings Q4 2025` in Telegram
- `session.error` → error, kills the process
- `result` → completion signal

### 6. Real-Time Streaming to Telegram

The bot creates a single Telegram message and progressively edits it as chunks arrive (every 2 seconds to avoid rate limits). Tool calls appear inline as `⚡` lines. If the response exceeds Telegram's 4,096-character limit, overflow is split into new messages.

### 7. File Sync and Notification

After the agent finishes:
1. New files in the workspace are synced to Azure Blob Storage
2. A diff is computed to identify which files were created during this run
3. The bot sends a follow-up message with clickable links to each generated file
4. If `--email` was specified, the full output plus all generated file contents are emailed

## The Agent and Skill System (This Is the Real Magic)

Here's where it gets genuinely exciting. Instead of hard-coding what the AI does, you define agents and skills as *markdown files*. That's right — the entire behavior of a sophisticated multi-step analysis agent is defined in a `.md` file. No Python. No JavaScript. Just structured English with some YAML frontmatter.

### Agents (`.agent.md`)

An agent is a YAML frontmatter header + markdown body:

```yaml
---
name: stock-analysis-pro
description: Comprehensive 10-module stock analysis
argument-hint: AAPL at $242.50
tools: [edit, agent, search, web]
skills: [goldman-sachs-screener, morgan-stanley-dcf, bridgewater-risk, ...]
---

You are an elite equity research agent. When the user provides a stock ticker...
```

The `tools` field controls what the agent can do (file editing, web search, shell commands, invoking sub-agents). The `skills` field references reusable instruction modules.

### Skills (`.skill.md`)

Skills are modular analysis components. The `stock-analysis-pro` agent has 10 skills, each responsible for one module of the analysis. The `real-estate-analysis` agent has 6. Skills can be shared across agents.

### CRUD API

Agents and skills are managed through REST endpoints: `GET /api/agents`, `POST /api/agents`, `PUT /api/agents/{name}`, `DELETE /api/agents/{name}`. You can create and edit them through the web dashboard or directly via the API.

## What's Next?

If you've read this far, you're either thinking "this is cool, how do I deploy it?" or "this is insane, wrapping a CLI subprocess in Docker?"

Either way, Part 2 is for you. I'll cover:
- **One-command deployment** — from zero to running in 5 minutes with `./deploy.sh`
- **The real cost** — spoiler: ~$5-8/month, and I'll show the exact breakdown
- **The cron system** — "run this agent every morning and email me" is as powerful as it sounds
- **Honest trade-offs** — what breaks, what's ugly, what I'd do differently
- **Security** — because running `--allow-all` in the cloud deserves a conversation

If you're the type who reads the limitations section first (I respect that), jump straight to Part 2.

---

*This is Part 1 of a 4-part series on OpenCopilot. [Part 2: The Nitty-Gritty →](part-2-design-deep-dive.md)*

**GitHub: [github.com/arjunpatel17/OpenCopilot](https://github.com/arjunpatel17/OpenCopilot)**
