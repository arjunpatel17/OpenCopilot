# OpenCopilot Setup Guide

Run GitHub Copilot agents and commands from a Telegram bot or API — deployed on Azure. The web dashboard provides a file explorer and real-time process log viewer.

---

## Prerequisites

- **macOS or Linux** (scripts use bash)
- **Azure CLI** (`az`) — [Install](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli)
- **GitHub CLI** (`gh`) — `brew install gh`
- **Python 3.12+** — [Install](https://www.python.org/downloads/)
- **Telegram account** — for the Telegram bot
- **GitHub account** with Copilot access
- **Azure subscription** — with permissions to create resources

---

## Step 1: Clone & Install

```bash
git clone <your-repo-url> OpenCopilot
cd OpenCopilot

# Create Python virtual environment
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..
```

---

## Step 2: Authenticate CLIs

### GitHub CLI
```bash
gh auth login --web --git-protocol https
```
Follow the browser prompt to authenticate.

### Azure CLI
```bash
az login
```
Select your subscription if prompted.

### Install Azure Container Apps extension
```bash
az extension add --name containerapp -y
```

---

## Step 3: Set Up Agents

Copy your agents and skills into the workspace directory:

```bash
mkdir -p workspace/.github/agents workspace/.github/skills

# If you have the GithubAgents repo:
cp /path/to/GithubAgents/agents/*.agent.md workspace/.github/agents/
cp /path/to/GithubAgents/skills/*.skill.md workspace/.github/skills/
```

Or create agents via the web UI after deployment.

---

## Step 4: Local Development (Optional)

To run locally before deploying to Azure:

```bash
# Copy the example env file
cp backend/.env.example backend/.env

# Edit .env — set WORKSPACE_DIR to your full workspace path:
#   WORKSPACE_DIR=/full/path/to/OpenCopilot/workspace

# Start the server
cd backend
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open **http://localhost:8000** in your browser to access the file explorer and process log viewer.

> **Note**: Copilot commands are triggered via Telegram or the API. The web UI shows generated files and live process logs.

---

## Step 5: Deploy to Azure

Run the deployment script from the project root:

```bash
./deploy.sh
```

This creates all Azure resources (no local Docker needed):
1. **Resource Group** — `opencopilot-rg`
2. **Azure Container Registry** — builds your Docker image in the cloud
3. **Azure Storage Account** — for files, reports, and chat sessions
4. **Azure Container Apps** — runs your app with a public URL

At the end it prints your public URL, e.g.:
```
https://opencopilot.<random>.eastus.azurecontainerapps.io
```

---

## Step 6: Set Up Telegram Bot

### Create a bot (one-time, ~30 seconds)

1. Open **Telegram** on your phone or desktop
2. Search for **@BotFather** and open the chat
3. Send `/newbot`
4. Enter a **display name** (e.g., `My Copilot Agent`)
5. Enter a **username** (must end in `bot`, e.g., `my_opencopilot_bot`)
6. BotFather replies with your **bot token** — copy it

### Run the setup script

```bash
# Interactive mode — prompts for token and username:
./setup-telegram.sh

# Or pass the token directly:
./setup-telegram.sh YOUR_BOT_TOKEN

# Or pass token + allowed username (non-interactive):
./setup-telegram.sh YOUR_BOT_TOKEN your_telegram_username
```

The script will:
- Verify your token with Telegram
- Update your local `.env`
- Set Azure Container App secrets
- Register the webhook
- Set bot commands (`/start`, `/agent`)

### Chat with your bot

1. Open the link printed by the script: `https://t.me/your_bot_username`
2. Press **Start**
3. Send any message — Copilot responds

---

## Telegram Bot Usage Guide

### Sending prompts

Just send any message to chat with GitHub Copilot. The bot streams the response in real time, editing a single message as text comes in. You'll see tool activity (file reads, web searches, shell commands) inline as `⚡` lines.

The bot maintains **conversation history** (last 10 turns per chat), so follow-up messages like "expand on that" or "now do the same for MSFT" will have context from prior exchanges.

### Running agents

Use `/agent` or shorthand `/agent-name` to run a specific agent with full tool access:

```
/agent stock-analysis AAPL at $242.50
/agent business-plan-analysis AI-powered resume builder SaaS
/agent book-creator A self-help book on building discipline
/real-estate-analysis 505 Regency Trl, Acworth GA
```

### Plan mode (read-only)

Use `/plan` for analysis without file edits or shell commands:

```
/plan Analyze the codebase and propose refactoring
/plan stock-analysis AAPL at $242.50
```

When an agent name is provided after `/plan`, it uses that agent's instructions in read-only mode.

### Other prompt commands

| Command | Description |
|---------|-------------|
| `/explain <prompt>` | Ask Copilot to explain code or concepts |
| `/suggest <prompt>` | Get a shell command suggestion |

### Model selection

Add `--model <id>` to any message to use a specific model:

```
--model claude-opus-4.6-1m explain quantum computing
/agent stock-analysis --model gpt-5.4 AAPL
```

### Introspection commands

| Command | Description |
|---------|-------------|
| `/files` | List all workspace files with clickable links to view/download each one |
| `/agents` | List available agents |
| `/skills` | List available skills |
| `/models` | List available AI models |
| `/mcps` | List configured MCP servers |
| `/version` | Show Copilot CLI version |

### Utility commands

| Command | Description |
|---------|-------------|
| `/clear` | Clear conversation history for the current chat |
| `/help` | Show the help menu |

### File output

When an agent generates files (reports, code, etc.), the bot sends a summary with **clickable links** to view each file directly. A link to the web file explorer is also included.

### Voice & image input

- **Voice messages**: Automatically transcribed and sent as text prompts (requires Azure Speech key)
- **Images**: Attached images are saved to the workspace and the agent is asked to analyze them

### Examples

```
hi, help me write a Python script
/agent stock-analysis AAPL at $242.50
/plan Analyze the codebase and propose refactoring
/explain What does asyncio.gather do?
/suggest find all Python files larger than 1MB
/files
/clear
```

---

## Updating After Code Changes

```bash
cd OpenCopilot

# Rebuild image in Azure (replace ACR name with yours)
az acr build --registry YOUR_ACR_NAME --image opencopilot:latest --file Dockerfile .

# Update the container app
az containerapp update \
  --resource-group opencopilot-rg \
  --name opencopilot \
  --image YOUR_ACR_NAME.azurecr.io/opencopilot:latest
```

The deploy script prints these exact commands with your ACR name.

---

## Tearing Down

To delete all Azure resources:

```bash
az group delete --name opencopilot-rg --yes --no-wait
```

---

## Project Structure

```
OpenCopilot/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app
│   │   ├── config.py            # Settings (env vars)
│   │   ├── auth.py              # Entra ID JWT auth
│   │   ├── routers/
│   │   │   ├── agents.py        # Agent CRUD API
│   │   │   ├── skills.py        # Skill CRUD API
│   │   │   ├── chat.py          # Chat (WebSocket + REST)
│   │   │   ├── files.py         # File explorer API
│   │   │   ├── logs.py          # Process logs (REST + WebSocket)
│   │   │   └── telegram.py      # Telegram webhook
│   │   ├── services/
│   │   │   ├── copilot.py       # Copilot CLI wrapper + activity log
│   │   │   ├── agent_parser.py  # .agent.md parser
│   │   │   ├── blob_storage.py  # Azure Blob / local storage
│   │   │   ├── response_parser.py
│   │   │   ├── session_manager.py
│   │   │   └── telegram_bot.py  # Telegram message handler
│   │   └── models/              # Pydantic models
│   ├── requirements.txt
│   └── .env                     # Local config (not committed)
├── frontend/
│   ├── index.html               # File explorer + process logs UI
│   ├── css/style.css            # VS Code dark theme
│   └── js/app.js                # File tree, logs WebSocket
├── workspace/
│   └── .github/
│       ├── agents/              # .agent.md files
│       └── skills/              # .skill.md files
├── Dockerfile                   # Container image
├── deploy.sh                    # Azure deployment script
├── setup-telegram.sh            # Telegram bot setup script
└── .gitignore
```

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Health check |
| `/api/agents` | GET, POST | List / create agents |
| `/api/agents/{name}` | GET, PUT, DELETE | Agent CRUD |
| `/api/skills` | GET, POST | List / create skills |
| `/api/skills/{name}` | GET, PUT, DELETE | Skill CRUD |
| `/api/chat` | POST | Synchronous chat |
| `/api/chat/stream` | WebSocket | Streaming chat |
| `/api/chat/sessions` | GET | List chat sessions |
| `/api/chat/sessions/{id}` | GET, DELETE | Session CRUD |
| `/api/files` | GET | List files |
| `/api/files/tree` | GET | File tree |
| `/api/files/content/{path}` | GET | File content |
| `/api/files/upload` | POST | Upload file |
| `/api/files/download/{path}` | GET | Download file/folder |
| `/api/files/{path}` | DELETE | Delete file |
| `/api/logs/snapshot` | GET | Current log buffer + active process |
| `/api/logs/stream` | WebSocket | Live process log stream |
| `/api/telegram/webhook` | POST | Telegram webhook |
| `/api/telegram/setup-webhook` | POST | Register webhook |
| `/api/telegram/webhook` | DELETE | Remove webhook |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `WORKSPACE_DIR` | Yes | Path to workspace with agents/skills |
| `AZURE_STORAGE_CONNECTION_STRING` | For Azure | Blob storage connection string |
| `AZURE_STORAGE_CONTAINER` | No | Storage container name (default: `copilot-files`) |
| `GH_TOKEN` | For Azure | GitHub token for `gh copilot` in container |
| `AUTH_ENABLED` | No | Enable Entra ID auth (default: `false`) |
| `AZURE_TENANT_ID` | If auth | Entra ID tenant |
| `AZURE_CLIENT_ID` | If auth | Entra ID client ID |
| `TELEGRAM_BOT_TOKEN` | For Telegram | Bot token from @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | For Telegram | Webhook verification secret |
| `TELEGRAM_ALLOWED_USERS` | No | JSON array of allowed Telegram usernames |
| `CORS_ORIGINS` | No | Allowed CORS origins (default: `["*"]`) |
