#!/bin/bash
# Applies the scale configuration for the OpenCopilot container app.
#
# Both rules are required:
#   http-requests   - wakes the app for dashboard, Telegram and webhook traffic.
#   agent-run-queue - keeps a replica alive for the full duration of an agent
#                     run. The run message stays in the queue (invisible, with
#                     its visibility timeout renewed) until the run finishes, so
#                     KEDA cannot scale to zero mid-run.
#
# The HTTP rule alone cannot do this: Container Apps closes any HTTP request
# idle for 240s, the concurrent-request metric then drops to zero, and the
# replica is deactivated after the cooldown, killing the in-flight agent.
#
# This lives in its own script because deploy.sh AND update.sh must both apply
# it. update.sh previously did not, which silently reverted the settings.
set -euo pipefail

RESOURCE_GROUP="${1:?usage: apply-scale.sh <resource-group> <app-name> <storage-account> [queue-name]}"
APP_NAME="${2:?missing app name}"
STORAGE_ACCOUNT="${3:?missing storage account name}"
QUEUE_NAME="${4:-agent-runs}"

SCALE_YAML=$(mktemp)
trap 'rm -f "$SCALE_YAML"' EXIT

cat > "$SCALE_YAML" <<EOF
properties:
  template:
    terminationGracePeriodSeconds: 600
    scale:
      minReplicas: 0
      maxReplicas: 1
      cooldownPeriod: 300
      pollingInterval: 30
      rules:
        - name: http-requests
          http:
            metadata:
              concurrentRequests: "10"
        - name: agent-run-queue
          custom:
            type: azure-queue
            metadata:
              accountName: $STORAGE_ACCOUNT
              queueName: $QUEUE_NAME
              queueLength: "1"
            auth:
              - secretRef: storage-conn
                triggerParameter: connection
EOF

az containerapp update \
    --resource-group "$RESOURCE_GROUP" \
    --name "$APP_NAME" \
    --yaml "$SCALE_YAML" \
    --output none

# Verify. A silent revert here is exactly the failure this script exists to fix.
RULES=$(az containerapp show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$APP_NAME" \
    --query "properties.template.scale.rules[].name" -o tsv 2>/dev/null || true)

if ! grep -q "agent-run-queue" <<< "$RULES"; then
    echo "ERROR: the agent-run-queue scale rule was not applied."
    echo "       Current rules: ${RULES:-none}"
    echo "       Long-running agent runs will be killed by scale-to-zero."
    exit 1
fi

echo "    Scale rules applied: $(tr '\n' ' ' <<< "$RULES")"
