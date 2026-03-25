#!/bin/bash
set -euo pipefail

# ============================================================
# Update OpenCopilot deployment on Azure Container Apps
# Rebuilds the image in ACR and restarts the container app
# ============================================================

RESOURCE_GROUP="opencopilot-rg"
CONTAINER_APP_NAME="opencopilot"
IMAGE_NAME="opencopilot"

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
echo ">>> Step 1/2: Building new image in ACR (this takes a few minutes)..."
az acr build \
    --registry "$ACR_NAME" \
    --image "${IMAGE_NAME}:latest" \
    --file Dockerfile \
    . \
    --no-logs

echo "    Image built: $ACR_NAME.azurecr.io/${IMAGE_NAME}:latest"

# Step 2: Update the container app to use the new image (force new revision)
echo ">>> Step 2/2: Updating container app..."
az containerapp update \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --image "$ACR_NAME.azurecr.io/${IMAGE_NAME}:latest" \
    --revision-suffix "deploy-$(date +%s)" \
    --output none

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
echo ""
