#!/bin/bash
set -euo pipefail

# ============================================================
# Deploy OpenCopilot to Azure Container Apps
# No local Docker required — builds in the cloud via ACR Tasks
# ============================================================

# ---------- Configuration ----------
RESOURCE_GROUP="opencopilot-rg"
LOCATION="eastus"
ACR_NAME="opencopilotacr$(openssl rand -hex 3)"  # must be globally unique
CONTAINER_APP_NAME="opencopilot"
CONTAINER_ENV_NAME="opencopilot-env"
IMAGE_NAME="opencopilot"
STORAGE_ACCOUNT_NAME="opencopilotsa$(openssl rand -hex 3)"
STORAGE_CONTAINER="copilot-files"

# GitHub token for gh copilot in the container
GH_TOKEN=$(gh auth token 2>/dev/null)
if [[ -z "$GH_TOKEN" ]]; then
    echo "ERROR: Not logged into GitHub CLI. Run: gh auth login"
    exit 1
fi

echo "=== OpenCopilot Azure Deployment ==="
echo "Resource Group: $RESOURCE_GROUP"
echo "Location:       $LOCATION"
echo ""

# ---------- Step 1: Create Resource Group ----------
echo ">>> Step 1/7: Creating resource group..."
az group create \
    --name "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --output none

# ---------- Step 2: Create Azure Container Registry ----------
echo ">>> Step 2/7: Creating container registry..."
az acr create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$ACR_NAME" \
    --sku Basic \
    --admin-enabled true \
    --output none

echo "    ACR: $ACR_NAME.azurecr.io"

# ---------- Step 3: Build image in ACR (no local Docker needed) ----------
echo ">>> Step 3/7: Building container image in Azure (this takes a few minutes)..."
az acr build \
    --registry "$ACR_NAME" \
    --image "${IMAGE_NAME}:latest" \
    --file Dockerfile \
    . \
    --no-logs

echo "    Image: $ACR_NAME.azurecr.io/${IMAGE_NAME}:latest"

# ---------- Step 4: Create Storage Account ----------
echo ">>> Step 4/7: Creating storage account..."
az storage account create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$STORAGE_ACCOUNT_NAME" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    --output none

STORAGE_CONNECTION_STRING=$(az storage account show-connection-string \
    --resource-group "$RESOURCE_GROUP" \
    --name "$STORAGE_ACCOUNT_NAME" \
    --query connectionString -o tsv)

az storage container create \
    --name "$STORAGE_CONTAINER" \
    --connection-string "$STORAGE_CONNECTION_STRING" \
    --output none

echo "    Storage: $STORAGE_ACCOUNT_NAME"

# ---------- Step 5: Create Container Apps Environment ----------
echo ">>> Step 5/7: Creating Container Apps environment..."
az containerapp env create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_ENV_NAME" \
    --location "$LOCATION" \
    --output none

# ---------- Step 6: Get ACR credentials ----------
echo ">>> Step 6/7: Getting registry credentials..."
ACR_USERNAME=$(az acr credential show --name "$ACR_NAME" --query username -o tsv)
ACR_PASSWORD=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)

# ---------- Step 7: Deploy Container App ----------
echo ">>> Step 7/7: Deploying container app..."
az containerapp create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --environment "$CONTAINER_ENV_NAME" \
    --image "$ACR_NAME.azurecr.io/${IMAGE_NAME}:latest" \
    --registry-server "$ACR_NAME.azurecr.io" \
    --registry-username "$ACR_USERNAME" \
    --registry-password "$ACR_PASSWORD" \
    --target-port 8000 \
    --ingress external \
    --min-replicas 0 \
    --max-replicas 1 \
    --cpu 1 \
    --memory 2Gi \
    --env-vars \
        "GH_TOKEN=secretref:gh-token" \
        "AZURE_STORAGE_CONNECTION_STRING=secretref:storage-conn" \
        "AZURE_STORAGE_CONTAINER=$STORAGE_CONTAINER" \
        "WORKSPACE_DIR=/workspace" \
        "AUTH_ENABLED=false" \
        "CORS_ORIGINS=[\"*\"]" \
    --secrets \
        "gh-token=$GH_TOKEN" \
        "storage-conn=$STORAGE_CONNECTION_STRING" \
    --output none

# ---------- Get the app URL ----------
APP_URL=$(az containerapp show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --query "properties.configuration.ingress.fqdn" -o tsv)

echo ""
echo "============================================"
echo "  DEPLOYMENT COMPLETE!"
echo "============================================"
echo ""
echo "  App URL:  https://$APP_URL"
echo ""
echo "  Resource Group:    $RESOURCE_GROUP"
echo "  Container Registry: $ACR_NAME"
echo "  Storage Account:   $STORAGE_ACCOUNT_NAME"
echo ""
echo "  To update after code changes:"
echo "    az acr build --registry $ACR_NAME --image ${IMAGE_NAME}:latest --file Dockerfile ."
echo "    az containerapp update --resource-group $RESOURCE_GROUP --name $CONTAINER_APP_NAME --image $ACR_NAME.azurecr.io/${IMAGE_NAME}:latest"
echo ""
echo "  To tear down everything:"
echo "    az group delete --name $RESOURCE_GROUP --yes --no-wait"
echo ""
