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
az containerapp update \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --image "$ACR_NAME.azurecr.io/${IMAGE_NAME}:latest" \
    --revision-suffix "deploy-$(date +%s)" \
    --output none

# Step 2a: Re-apply optional API keys from backend/.env so a rotated or
# newly-added key flows to Azure on the next update — no need to re-run deploy.sh.
# Mirrors the wiring step in deploy.sh.
ENV_FILE_FOR_KEYS="$(dirname "$0")/backend/.env"
if [[ -f "$ENV_FILE_FOR_KEYS" ]]; then
    FINNHUB_API_KEY=$(grep '^FINNHUB_API_KEY=' "$ENV_FILE_FOR_KEYS" | cut -d= -f2- | tr -d '[:space:]')
    if [[ -n "${FINNHUB_API_KEY:-}" ]]; then
        echo "    Syncing FINNHUB_API_KEY from backend/.env..."
        az containerapp secret set \
            --resource-group "$RESOURCE_GROUP" \
            --name "$CONTAINER_APP_NAME" \
            --secrets "finnhub-key=$FINNHUB_API_KEY" \
            --output none

        az containerapp update \
            --resource-group "$RESOURCE_GROUP" \
            --name "$CONTAINER_APP_NAME" \
            --set-env-vars "FINNHUB_API_KEY=secretref:finnhub-key" \
            --output none
    fi
fi

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
