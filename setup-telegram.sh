#!/bin/bash
set -euo pipefail

# ============================================================
# Telegram Bot Setup Script for OpenCopilot
# Creates a bot via BotFather API, configures webhook, and
# updates Azure Container App secrets.
# ============================================================

# ---------- Configuration ----------
# Update APP_URL after running deploy.sh (it prints the URL)
APP_URL="${APP_URL:-}"
RESOURCE_GROUP="opencopilot-rg"
CONTAINER_APP="opencopilot"
ENV_FILE="$(dirname "$0")/backend/.env"
WEBHOOK_SECRET="opencopilot-$(openssl rand -hex 8)"

if [[ -z "$APP_URL" ]]; then
    echo "ERROR: APP_URL is not set. Pass it as an environment variable:"
    echo "       APP_URL=https://your-app.eastus.azurecontainerapps.io ./setup-telegram.sh"
    echo "       Run deploy.sh first to get your URL."
    exit 1
fi

echo "============================================"
echo "  Telegram Bot Setup for OpenCopilot"
echo "============================================"
echo ""

# ---------- Step 1: Get bot token ----------
# Accept token as argument or prompt for it
if [[ $# -ge 1 ]]; then
    BOT_TOKEN="$1"
    echo ">>> Step 1: Using provided bot token"
else
    echo ">>> Step 1: Bot Token"
    echo ""
    echo "  You need to create a bot via Telegram's @BotFather:"
    echo ""
    echo "  1. Open Telegram and search for @BotFather"
    echo "  2. Send /newbot"
    echo "  3. Choose a display name (e.g., 'OpenCopilot')"
    echo "  4. Choose a username (must end in 'bot', e.g., 'my_opencopilot_bot')"
    echo "  5. BotFather will give you a token like: 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    echo ""
    read -rp "  Paste your bot token here: " BOT_TOKEN
fi

# Accept allowed user as second argument or prompt
if [[ $# -ge 2 ]]; then
    ALLOWED_USER="$2"
else
    ALLOWED_USER=""
fi

if [[ -z "$BOT_TOKEN" ]]; then
    echo "ERROR: No token provided."
    exit 1
fi

# Validate token format
if [[ ! "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
    echo "ERROR: Token format looks invalid. Expected format: 123456:ABC-DEF..."
    exit 1
fi

# ---------- Step 2: Verify the token works ----------
echo ""
echo ">>> Step 2: Verifying token..."
BOT_INFO=$(curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getMe")
BOT_OK=$(echo "$BOT_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok', False))" 2>/dev/null || echo "False")

if [[ "$BOT_OK" != "True" ]]; then
    echo "ERROR: Token is invalid. Response: $BOT_INFO"
    exit 1
fi

BOT_USERNAME=$(echo "$BOT_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['username'])")
BOT_NAME=$(echo "$BOT_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['first_name'])")
echo "  ✓ Bot verified: @${BOT_USERNAME} (${BOT_NAME})"

# ---------- Step 3: Optional - restrict to your username ----------
if [[ -z "$ALLOWED_USER" ]]; then
    echo ""
    read -rp "  Restrict bot to your Telegram username? (leave blank to allow everyone): @" ALLOWED_USER
fi

ALLOWED_USERS_JSON="[]"
if [[ -n "$ALLOWED_USER" ]]; then
    ALLOWED_USERS_JSON="[\"${ALLOWED_USER}\"]"
    echo "  ✓ Only @${ALLOWED_USER} can use the bot"
else
    echo "  ✓ Anyone can use the bot"
fi

# ---------- Step 4: Update local .env ----------
echo ""
echo ">>> Step 3: Updating local .env..."

if [[ -f "$ENV_FILE" ]]; then
    # Remove existing telegram lines if present
    sed -i '' '/^TELEGRAM_BOT_TOKEN=/d' "$ENV_FILE" 2>/dev/null || true
    sed -i '' '/^TELEGRAM_ALLOWED_USERS=/d' "$ENV_FILE" 2>/dev/null || true
    sed -i '' '/^TELEGRAM_WEBHOOK_SECRET=/d' "$ENV_FILE" 2>/dev/null || true
    sed -i '' '/^# Telegram Bot$/d' "$ENV_FILE" 2>/dev/null || true

    # Append new values
    cat >> "$ENV_FILE" << EOF

# Telegram Bot
TELEGRAM_BOT_TOKEN=${BOT_TOKEN}
TELEGRAM_ALLOWED_USERS=${ALLOWED_USERS_JSON}
TELEGRAM_WEBHOOK_SECRET=${WEBHOOK_SECRET}
EOF
    echo "  ✓ Updated ${ENV_FILE}"
else
    echo "  ⚠ .env file not found at ${ENV_FILE}, skipping local config"
fi

# ---------- Step 5: Update Azure secrets ----------
echo ""
echo ">>> Step 4: Updating Azure Container App secrets..."

az containerapp secret set \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP" \
    --secrets \
        "telegram-token=${BOT_TOKEN}" \
        "telegram-secret=${WEBHOOK_SECRET}" \
    --output none 2>/dev/null

echo "  ✓ Secrets updated in Azure"

# ---------- Step 6: Update Azure env vars and restart ----------
echo ""
echo ">>> Step 5: Updating container environment variables..."

az containerapp update \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP" \
    --set-env-vars \
        "TELEGRAM_BOT_TOKEN=secretref:telegram-token" \
        "TELEGRAM_WEBHOOK_SECRET=secretref:telegram-secret" \
    --output none 2>/dev/null

echo "  ✓ Container updated and restarting"

# Wait for container to be ready
echo "  Waiting for container to start..."
sleep 15

# ---------- Step 7: Set up webhook ----------
echo ""
echo ">>> Step 6: Registering Telegram webhook..."

WEBHOOK_URL="${APP_URL}/api/telegram/webhook"
WEBHOOK_RESULT=$(curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook?url=${WEBHOOK_URL}&secret_token=${WEBHOOK_SECRET}")
WEBHOOK_OK=$(echo "$WEBHOOK_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok', False))" 2>/dev/null || echo "False")

if [[ "$WEBHOOK_OK" != "True" ]]; then
    echo "  ⚠ Webhook setup failed: $WEBHOOK_RESULT"
    echo "  You can retry manually:"
    echo "    curl -X POST \"https://api.telegram.org/bot\${BOT_TOKEN}/setWebhook?url=${WEBHOOK_URL}&secret_token=${WEBHOOK_SECRET}\""
else
    echo "  ✓ Webhook registered: ${WEBHOOK_URL}"
fi

# ---------- Step 8: Set bot commands in Telegram ----------
echo ""
echo ">>> Step 7: Setting bot commands..."

curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setMyCommands" \
    -H "Content-Type: application/json" \
    -d '{
        "commands": [
            {"command": "start", "description": "Show help and usage info"},
            {"command": "agent", "description": "Run an agent: /agent name prompt"}
        ]
    }' > /dev/null 2>&1

echo "  ✓ Bot commands registered"

# ---------- Done ----------
echo ""
echo "============================================"
echo "  TELEGRAM BOT SETUP COMPLETE!"
echo "============================================"
echo ""
echo "  Bot:      @${BOT_USERNAME}"
echo "  Link:     https://t.me/${BOT_USERNAME}"
echo "  Webhook:  ${WEBHOOK_URL}"
if [[ -n "$ALLOWED_USER" ]]; then
echo "  Allowed:  @${ALLOWED_USER}"
fi
echo ""
echo "  How to use:"
echo "    1. Open https://t.me/${BOT_USERNAME}"
echo "    2. Press Start"
echo "    3. Send any message to chat with Copilot"
echo "    4. Use /agent agent-name prompt to run agents"
echo ""
echo "  Examples:"
echo "    • hi"
echo "    • /agent stock-analysis MSFT 383"
echo "    • /agent business-plan-analysis AI resume builder"
echo ""
