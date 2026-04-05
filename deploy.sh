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

# ---------- Step 0: Ensure submodules are initialized ----------
echo ">>> Step 0: Initializing submodules..."
git submodule update --init --recursive 2>/dev/null || echo "    Submodule not available — deploying without workspace content"
mkdir -p workspace/.github/agents workspace/.github/skills workspace/tools

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
    --min-tls-version TLS1_2 \
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
        "AUTH_ENABLED=true" \
    --secrets \
        "gh-token=$GH_TOKEN" \
        "storage-conn=$STORAGE_CONNECTION_STRING" \
    --output none

# Set a longer scale cooldown (30 min) so long-running agent tasks over
# WebSocket don't get killed by a premature scale-to-zero.
SCALE_YAML=$(mktemp)
cat > "$SCALE_YAML" <<EOF
properties:
  template:
    scale:
      cooldownPeriod: 1800
      minReplicas: 0
      maxReplicas: 1
EOF
az containerapp update \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --yaml "$SCALE_YAML" \
    --output none
rm -f "$SCALE_YAML"

# ---------- Get the app URL ----------
APP_URL=$(az containerapp show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --query "properties.configuration.ingress.fqdn" -o tsv)

# ---------- Step 8/10: Deploy Azure Function (cron timer) ----------
echo ">>> Step 8/10: Deploying cron timer function..."
FUNC_APP_NAME="opencopilot-cron"
FUNC_STORAGE_NAME="opencopilotcronsa"
CRON_SECRET=$(openssl rand -hex 16)

# Create storage account for the function app
az storage account create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$FUNC_STORAGE_NAME" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    --min-tls-version TLS1_2 \
    --output none

# Create Function App (Consumption plan, Python 3.12)
az functionapp create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$FUNC_APP_NAME" \
    --storage-account "$FUNC_STORAGE_NAME" \
    --consumption-plan-location "$LOCATION" \
    --runtime python \
    --runtime-version 3.12 \
    --functions-version 4 \
    --os-type Linux \
    --https-only true \
    --output none

# Set function app settings
az functionapp config appsettings set \
    --resource-group "$RESOURCE_GROUP" \
    --name "$FUNC_APP_NAME" \
    --settings \
        "CONTAINER_APP_URL=https://$APP_URL" \
        "CRON_SECRET=$CRON_SECRET" \
    --output none

# Deploy function code
pushd azure-function > /dev/null
func azure functionapp publish "$FUNC_APP_NAME" --python 2>/dev/null || {
    echo "    NOTE: Install Azure Functions Core Tools to auto-deploy the function."
    echo "    Run: brew install azure-functions-core-tools@4"
    echo "    Then: cd azure-function && func azure functionapp publish $FUNC_APP_NAME --python"
}
popd > /dev/null

# Also set CRON_SECRET on the container app so it can verify requests
az containerapp secret set \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --secrets "cron-secret=$CRON_SECRET" \
    --output none

az containerapp update \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --set-env-vars "CRON_SECRET=secretref:cron-secret" \
    --output none

echo "    Function App: $FUNC_APP_NAME"
echo "    Cron Secret:  (auto-generated and set on both function + container app)"

# ---------- Step 9/10: Create Azure Communication Services (email) ----------
echo ">>> Step 9/10: Setting up email service..."
COMM_NAME="opencopilot-comm"
EMAIL_SVC_NAME="opencopilot-email"

az communication create \
    --name "$COMM_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location global \
    --data-location unitedstates \
    --output none

az communication email create \
    --name "$EMAIL_SVC_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location global \
    --data-location unitedstates \
    --output none 2>/dev/null

az communication email domain create \
    --domain-name AzureManagedDomain \
    --email-service-name "$EMAIL_SVC_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location global \
    --domain-management azuremanaged \
    --output none 2>/dev/null

# Link email domain to communication service
DOMAIN_ID=$(az communication email domain show \
    --domain-name AzureManagedDomain \
    --email-service-name "$EMAIL_SVC_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query "id" -o tsv)

az communication update \
    --name "$COMM_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --linked-domains "$DOMAIN_ID" \
    --output none

# Get connection string and sender address
COMM_CONN_STRING=$(az communication list-key \
    --name "$COMM_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query "primaryConnectionString" -o tsv)

EMAIL_DOMAIN=$(az communication email domain show \
    --domain-name AzureManagedDomain \
    --email-service-name "$EMAIL_SVC_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query "fromSenderDomain" -o tsv)

EMAIL_SENDER="DoNotReply@${EMAIL_DOMAIN}"

echo "    Email sender: $EMAIL_SENDER"

# ---------- Step 10/10: Set email env vars on container app ----------
echo ">>> Step 10/10: Configuring email on container app..."
az containerapp secret set \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --secrets "comm-conn=$COMM_CONN_STRING" \
    --output none

az containerapp update \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_APP_NAME" \
    --set-env-vars \
        "AZURE_COMM_CONNECTION_STRING=secretref:comm-conn" \
        "EMAIL_SENDER_ADDRESS=$EMAIL_SENDER" \
    --output none

echo "    Email configured on container app"

echo ""
echo "============================================"
echo "  DEPLOYMENT COMPLETE!"
echo "============================================"
echo ""
echo "  App URL:  https://$APP_URL"
echo ""
echo "  Resource Group:     $RESOURCE_GROUP"
echo "  Container Registry: $ACR_NAME"
echo "  Storage Account:    $STORAGE_ACCOUNT_NAME"
echo "  Function App:       $FUNC_APP_NAME"
echo "  Email Sender:       $EMAIL_SENDER"
echo ""
echo "  To update after code changes:  ./update.sh"
echo ""
echo "  To tear down everything:"
echo "    az group delete --name $RESOURCE_GROUP --yes --no-wait"
echo ""
