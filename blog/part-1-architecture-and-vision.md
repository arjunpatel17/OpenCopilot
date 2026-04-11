# OpenCopilot - GitHub Copilot on the Cloud

## Introduction

If you've used GitHub Copilot's standalone CLI (`@github/copilot`), you know how powerful it is. You can spin up custom agents with `.agent.md` files, give them web search and shell access, chain skills together, and get structured JSONL output.

But there's a catch - it only runs on your local machine.

That means:
- You can't trigger it without your laptop
- You can't talk to it
- You can't schedule agents (like stock analysis to run every morning at 8 AM and email you the report)
- You can't use a variety of LLM models to do the analysis
- Long-running agent tasks (10+ minutes of web searches, analysis, file generation) tie up your terminal and your time

I wanted to solve all of that. The idea was simple: wrap the Copilot CLI in a cloud service, expose it through a REST API and Telegram bot, add file storage and scheduling, and deploy the whole thing to Azure for ~$0/month when idle.

That's OpenCopilot.

## What OpenCopilot Does

OpenCopilot is a cloud-native application that lets you run GitHub Copilot agents via three interfaces:

1. **A web dashboard** - a VS Code-inspired dark-themed UI with a file explorer and real-time process log viewer
2. **REST and WebSocket APIs** - for programmatic access and real-time streaming
3. **A Telegram bot** - chat with your agents from your phone, complete with voice transcription, image analysis, and inline file links

Here's what a typical interaction looks like:

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

The agent runs on a container in the cloud. You get the result on your phone. The generated report files are stored in Azure Blob Storage and accessible through the web dashboard or via your phone.

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

## Why These Choices?

### Why Copilot CLI over Claude Code?

This was the biggest architectural decision. Claude Code (Anthropic's CLI agent) is excellent - arguably the most capable single-model coding agent available. But for this project, Copilot CLI won on several axes:

1. **Model flexibility** — Copilot CLI gives you access to multiple frontier models (Claude, GPT-4o, Gemini, o4-mini) through a single interface. You're not locked into one provider. If Claude Opus is best for deep analysis but GPT-4o is faster for simple lookups, you can pick per-agent or per-request with `--model`. Claude Code only runs Claude.

2. **Cost structure** — If you already have a GitHub Copilot subscription (which most developers do), you get model access included. Claude Code requires a separate Anthropic API key and you pay per token. For a personal tool that runs scheduled jobs daily, those API costs add up. With Copilot, the marginal cost of running an agent is $0.

3. **Built-in tool infrastructure** — Web search, file I/O, shell execution, and sub-agent invocation are all built into the Copilot CLI. With Claude Code, you'd need to wire up MCP servers or custom tool integrations for equivalent functionality.

4. **Structured output** — The `--output-format json` flag gives you a clean JSONL event stream (message deltas, tool calls, errors, results) that's trivial to parse and forward to Telegram or a WebSocket. This made real-time streaming straightforward without custom parsing of terminal escape codes.

5. **GitHub ecosystem integration** — The CLI authenticates via `gh auth token`, which the deployment script already captures. No separate API key management, no token rotation headaches, no billing surprises.

### Why wrap the CLI instead of calling the API directly?

The copilot CLI is the only way to get access to the full agent system - `.agent.md` files, skills, tool orchestration, MCP servers, multi-turn conversations. The API endpoints for Copilot models exist, but they don't give you the agent runtime. By wrapping the CLI, I get all of that for free: the agent discovers skills, decides which tools to invoke, reads and writes files, does web searches - all orchestrated by the CLI.

### Why Azure Container Apps?

Scale to zero. When nobody is using the bot, no containers run and the cost is $0. When a Telegram message arrives, the container spins up in seconds, processes the request, and scales back down after 30 minutes of inactivity. For a personal project, this is ideal.

### Why Telegram instead of a custom chat UI?

I already carry my phone everywhere. Building a full chat UI (with message history, typing indicators, mobile responsiveness) is weeks of work. Telegram gives me all of that for free, plus voice messages, image sharing, and push notifications. The web dashboard exists for file browsing and log monitoring, not for chatting.

## The Core Flow: How a Chat Message Becomes a Report

Let's trace what happens when you send `/agent stock-analysis-pro AAPL at $198.50` to the Telegram bot:

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
{"type": "tool.execution_start", "data": {"toolName": "write", "arguments": {"path": "reports/AAPL-01-stock-screener.md"}}}
{"type": "assistant.message_delta", "data": {"deltaContent": "Stock analysis complete..."}}
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

## The Agent and Skill System

This is where OpenCopilot gets interesting. Instead of hard-coding what the AI does, you define agents and skills as markdown files.

### Agents (`.agent.md`)

An agent is a YAML frontmatter header + markdown body:

```yaml
---
name: stock-analysis-pro
description: Comprehensive 10-module stock analysis
argument-hint: AAPL at $242.50
tools: [edit, agent, search, web]
skills: [stock-screener, stock-dcf, stock-risk, ...]
---

You are an elite equity research agent. When the user provides a stock ticker...
```

The `tools` field controls what the agent can do (file editing, web search, shell commands, invoking sub-agents). The `skills` field references reusable instruction modules.

### Skills (`.skill.md`)

Skills are modular analysis components. The `stock-analysis-pro` agent has 10 skills, each responsible for one module of the analysis. The `real-estate-analysis` agent has 6. Skills can be shared across agents.

### CRUD API

Agents and skills are managed through REST endpoints: `GET /api/agents`, `POST /api/agents`, `PUT /api/agents/{name}`, `DELETE /api/agents/{name}`. You can create and edit them through the web dashboard or directly via the API.

## Conclusion

I started this project because I was tired of copying stock tickers into ChatGPT every morning. That's it. No grand vision, no startup pitch - just a guy who wanted his phone to buzz at 7 AM with a stock report he didn't have to think about.

One weekend of hacking turned into two, which turned into "wait, what if I add scheduling," which turned into "okay now I need email delivery," which turned into… this. The scope creep was real, but so was the dopamine hit every time I woke up to a Telegram message with analysis that would've taken me an hour to do manually.

Now the portfolio advisor runs every morning before I'm awake. My real estate due diligence that used to eat a weekend runs in 3 minutes from a text message. My job search stays current without me lifting a finger. I have some agents that are useful, some experimental.

Is wrapping a CLI subprocess in Docker elegant? Absolutely not. Is parsing JSONL from stdout at 2 AM when a zombie process crashes your container fun? Also no. But you know what *is* fun? Texting your phone and getting back a Wall Street-grade equity report while you're still in bed. That trade-off works for me every single time.

If any of this sounds useful to you, the repo is open source. Clone it, break it, build agents that solve problems I never thought of. I'd love to see what you come up with.

**GitHub: [github.com/arjunpatel17/OpenCopilot](https://github.com/arjunpatel17/OpenCopilot)**
