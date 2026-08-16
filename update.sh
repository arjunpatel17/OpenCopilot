#!/bin/bash
set -euo pipefail

# ============================================================
# Update OpenCopilot deployment on Azure Container Apps
# Rebuilds the image in ACR and restarts the container app
# ============================================================

RESOURCE_GROUP="opencopilot-rg"
CONTAINER_APP_NAME="opencopilot"
IMAGE_NAME="opencopilot"
FUNC_APP_NAME="opencopilot-cron"
# Must match AGENT_QUEUE in deploy.sh.
AGENT_QUEUE="agent-runs"
# Copilot LLM model used for agent runs. Must be a model available to the
# Copilot CLI for the GH_TOKEN account (see https://api.githubcopilot.com/models).
# Avoid "-internal" variants — those are gated to GitHub employees only.
COPILOT_MODEL="claude-opus-4.8-1m"

# Auto-detect ACR name from the running container app
echo ">>> Detecting current deployment..."
CURRENT_IMAGE=$(az containerapp show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --query "properties.template.containers[0].image" -o tsv 2>/dev/null)

if [[ -z "$CURRENT_IMAGE" ]]; then
    echo "ERROR: Container app '$CONTAINER_APP_NAME' not found in resource group '$RESOURCE_GROUP'."
    echo "Run deploy.sh for initial deployment."
    exit 1
fi

ACR_NAME=$(echo "$CURRENT_IMAGE" | cut -d'.' -f1)
echo "    Container App: $CONTAINER_APP_NAME"
echo "    ACR:           $ACR_NAME"
echo "    Current Image: $CURRENT_IMAGE"
echo ""

# Step 1: Rebuild image in ACR
echo ">>> Step 1/3: Building new image in ACR (this takes a few minutes)..."
az acr build \
    --registry "$ACR_NAME" \
    --image "${IMAGE_NAME}:latest" \
    --file Dockerfile \
    . \
    --no-logs

echo "    Image built: $ACR_NAME.azurecr.io/${IMAGE_NAME}:latest"

# Step 2: Update the container app to use the new image (force new revision)
echo ">>> Step 2/3: Updating container app..."
# Use uuidgen for the suffix. Even with a unique suffix, `az containerapp
# update` occasionally returns 'revision with suffix ... already exists'
# *after* successfully creating the revision (looks like an internal retry
# in the CLI colliding with its own first attempt). Verify the rollout
# ourselves after the call and treat the spurious error as success when
# the suffix shows up in the revision list.
REV_SUFFIX="deploy-$(date +%s)-$(uuidgen 2>/dev/null | tr 'A-Z' 'a-z' | cut -c1-8 || openssl rand -hex 4)"
UPDATE_OUT=$(az containerapp update \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --image "$ACR_NAME.azurecr.io/${IMAGE_NAME}:latest" \
    --revision-suffix "$REV_SUFFIX" \
    --output none 2>&1) || {
    # Confirm whether the revision was actually created despite the error.
    if az containerapp revision show \
        --resource-group "$RESOURCE_GROUP" \
        --name "$CONTAINER_APP_NAME" \
        --revision "${CONTAINER_APP_NAME}--${REV_SUFFIX}" \
        --query "name" -o tsv >/dev/null 2>&1; then
        echo "    (CLI returned an error but revision $REV_SUFFIX is live; continuing)"
    else
        echo "    az containerapp update failed:"
        echo "$UPDATE_OUT"
        exit 1
    fi
}

# `az containerapp secret set` / `update` intermittently report "revision with
# suffix ... already exists" *after* successfully applying the change (an
# internal CLI retry racing its own first attempt). Under `set -e` that cosmetic
# error aborts the whole script — and because the scale config is applied near
# the end, the app is left on default scale rules with no queue rule.
az_tolerant() {
    local out
    if out=$(az "$@" 2>&1); then
        return 0
    fi
    if grep -q "already exists" <<< "$out"; then
        echo "    (ignoring spurious 'already exists' from az $1 $2)"
        return 0
    fi
    echo "$out" >&2
    return 1
}

# Step 2a: Refresh GH tokens from gh CLI so a re-auth on the host flows to
# Azure on the next update — important because the Copilot LLM lives on
# arjun-d-patel and tokens rotate. GH_REPO_TOKEN (arjunpatel17) is used for
# git operations via the gh wrapper in the Dockerfile.
GH_TOKEN_LATEST=$(gh auth token --user arjun-d-patel 2>/dev/null || true)
GH_REPO_TOKEN_LATEST=$(gh auth token --user arjunpatel17 2>/dev/null || true)
if [[ -n "$GH_TOKEN_LATEST" ]]; then
    echo "    Syncing GH_TOKEN (arjun-d-patel) for Copilot LLM..."
    az_tolerant containerapp secret set \
        --resource-group "$RESOURCE_GROUP" \
        --name "$CONTAINER_APP_NAME" \
        --secrets "gh-token=$GH_TOKEN_LATEST" \
        --output none
fi
if [[ -n "$GH_REPO_TOKEN_LATEST" ]]; then
    echo "    Syncing GH_REPO_TOKEN (arjunpatel17) for git ops..."
    az_tolerant containerapp secret set \
        --resource-group "$RESOURCE_GROUP" \
        --name "$CONTAINER_APP_NAME" \
        --secrets "gh-repo-token=$GH_REPO_TOKEN_LATEST" \
        --output none
fi

# Step 2b: Re-apply optional API keys from backend/.env so a rotated or
# newly-added key flows to Azure on the next update — no need to re-run deploy.sh.
# Mirrors the wiring step in deploy.sh.
ENV_FILE_FOR_KEYS="$(dirname "$0")/backend/.env"
if [[ -f "$ENV_FILE_FOR_KEYS" ]]; then
    FINNHUB_API_KEY=$(grep '^FINNHUB_API_KEY=' "$ENV_FILE_FOR_KEYS" | cut -d= -f2- | tr -d '[:space:]')
    if [[ -n "${FINNHUB_API_KEY:-}" ]]; then
        echo "    Syncing FINNHUB_API_KEY from backend/.env..."
        az_tolerant containerapp secret set \
            --resource-group "$RESOURCE_GROUP" \
            --name "$CONTAINER_APP_NAME" \
            --secrets "finnhub-key=$FINNHUB_API_KEY" \
            --output none

        az_tolerant containerapp update \
            --resource-group "$RESOURCE_GROUP" \
            --name "$CONTAINER_APP_NAME" \
            --set-env-vars "FINNHUB_API_KEY=secretref:finnhub-key" \
            --output none
    fi
fi

# Step 2b-model: Ensure COPILOT_MODEL env var matches the configured model so
# agent runs don't fall back to an unavailable default. Keep in sync with deploy.sh.
echo "    Syncing COPILOT_MODEL ($COPILOT_MODEL)..."
az_tolerant containerapp update \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --set-env-vars "COPILOT_MODEL=$COPILOT_MODEL" \
    --output none

# Step 2c: Ensure the agent run queue exists and re-apply scale config.
# `az containerapp update --image` resets scale settings to defaults, and a
# bare `cooldownPeriod` is silently dropped when no explicit scale rules are
# defined — which is why the previous 1200s setting never took effect. The
# queue rule is what actually keeps a replica alive for a whole agent run.
#
# Derive the storage account from the container app's own `storage-conn`
# secret. The resource group holds several storage accounts (function hosts,
# plus orphans from earlier deploys), so picking one by list order would point
# the queue and the scale rule at an account the backend never writes to.
STORAGE_CONNECTION_STRING=$(az containerapp secret show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --secret-name storage-conn \
    --query value -o tsv 2>/dev/null || true)

STORAGE_ACCOUNT_NAME=$(sed -n 's/.*AccountName=\([^;]*\).*/\1/p' <<< "$STORAGE_CONNECTION_STRING")

if [[ -z "$STORAGE_ACCOUNT_NAME" ]]; then
    echo "ERROR: could not read the storage account from the 'storage-conn' secret."
    echo "       Run deploy.sh first, or check: az containerapp secret list -g $RESOURCE_GROUP -n $CONTAINER_APP_NAME"
    exit 1
fi

echo "    Storage account: $STORAGE_ACCOUNT_NAME"
echo "    Ensuring agent run queue '$AGENT_QUEUE' exists..."
az storage queue create \
    --name "$AGENT_QUEUE" \
    --connection-string "$STORAGE_CONNECTION_STRING" \
    --output none

az containerapp update \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --set-env-vars "AGENT_QUEUE_NAME=$AGENT_QUEUE" \
    --output none

echo "    Applying scale configuration..."
"$(dirname "$0")/apply-scale.sh" \
    "$RESOURCE_GROUP" "$CONTAINER_APP_NAME" "$STORAGE_ACCOUNT_NAME" "$AGENT_QUEUE"

# Get the app URL
APP_URL=$(az containerapp show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --query "properties.configuration.ingress.fqdn" -o tsv)

echo ""
echo "============================================"
echo "  UPDATE COMPLETE!"
echo "============================================"
echo "  App URL: https://$APP_URL"

# Step 3: Update Azure Function (if it exists)
FUNC_EXISTS=$(az functionapp show --resource-group "$RESOURCE_GROUP" --name "$FUNC_APP_NAME" --query "name" -o tsv 2>/dev/null || true)
if [[ -n "$FUNC_EXISTS" ]]; then
    echo ""
    echo ">>> Step 3/3: Updating cron function..."
    pushd azure-function > /dev/null
    func azure functionapp publish "$FUNC_APP_NAME" --python 2>/dev/null || {
        echo "    NOTE: func CLI not installed. To update the function manually:"
        echo "    cd azure-function && func azure functionapp publish $FUNC_APP_NAME --python"
    }
    popd > /dev/null
    echo "    Function App updated: $FUNC_APP_NAME"
else
    echo ""
    echo "  (No cron function found — skipping. Run deploy.sh for initial setup.)"
fi
echo ""
