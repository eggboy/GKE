#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ -f "${ROOT_DIR}/.env" ]; then
  set -a
  # shellcheck source=/dev/null
  . "${ROOT_DIR}/.env"
  set +a
fi

export PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID in .env (copy .env.example) or in the environment}"
export REGION="${REGION:-us-central1}"
export CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS=false

EXT_VIP="$(kubectl get gateway p1-storefront-ext-gw -n p1-demo -o jsonpath='{.status.addresses[0].value}')"
EXT_FR_NAME="$(gcloud compute forwarding-rules list \
  --global \
  --filter="IPAddress=${EXT_VIP} AND loadBalancingScheme=EXTERNAL_MANAGED" \
  --format='value(name)' \
  --project="${PROJECT_ID}")"

RILB_VIP="$(kubectl get gateway p1-checkout-rilb-gw -n p1-demo -o jsonpath='{.status.addresses[0].value}')"
RILB_FR_NAME="$(gcloud compute forwarding-rules list \
  --regions="${REGION}" \
  --filter="IPAddress=${RILB_VIP} AND loadBalancingScheme=INTERNAL_MANAGED" \
  --format='value(name)' \
  --project="${PROJECT_ID}")"

echo "==> [Phase 3] Attaching TelemetryPolicy resources"
echo "    External VIP=${EXT_VIP} -> ForwardingRule=${EXT_FR_NAME}"
echo "    Internal VIP=${RILB_VIP} -> ForwardingRule=${RILB_FR_NAME}"

sed -e "s|PROJECT_ID|${PROJECT_ID}|g" \
    -e "s|EXT_FR_NAME|${EXT_FR_NAME}|g" \
    "${ROOT_DIR}/policies/telemetry-external.yaml.tmpl" > /tmp/p1-telemetry-external.yaml

gcloud beta network-services telemetry-policies import p1-storefront-tracing-policy \
  --source=/tmp/p1-telemetry-external.yaml \
  --location=global \
  --project="${PROJECT_ID}" \
  --quiet

sed -e "s|PROJECT_ID|${PROJECT_ID}|g" \
    -e "s|REGION|${REGION}|g" \
    -e "s|RILB_FR_NAME|${RILB_FR_NAME}|g" \
    "${ROOT_DIR}/policies/telemetry-internal.yaml.tmpl" > /tmp/p1-telemetry-internal.yaml

gcloud beta network-services telemetry-policies import p1-checkout-tracing-policy \
  --source=/tmp/p1-telemetry-internal.yaml \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --quiet

echo "==> Describing active TelemetryPolicies..."
gcloud beta network-services telemetry-policies describe p1-storefront-tracing-policy \
  --location=global --project="${PROJECT_ID}"
gcloud beta network-services telemetry-policies describe p1-checkout-tracing-policy \
  --location="${REGION}" --project="${PROJECT_ID}"
