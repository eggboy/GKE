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
export ZONE="${ZONE:-us-central1-a}"
export CLUSTER_NAME="${CLUSTER_NAME:-p1-lb-trace-gke}"
export CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS=false

echo "==> [Phase 2] Fetching GKE credentials for ${CLUSTER_NAME}..."
gcloud container clusters get-credentials "${CLUSTER_NAME}" \
  --zone="${ZONE}" \
  --dns-endpoint \
  --project="${PROJECT_ID}" \
  --quiet

echo "==> Verifying GKE Managed OpenTelemetry Instrumentation CRD..."
kubectl get crd instrumentations.telemetry.googleapis.com

if ! gcloud artifacts repositories describe p1-lb-trace-repo --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud artifacts repositories create p1-lb-trace-repo \
    --repository-format=docker \
    --location="${REGION}" \
    --description="GKE LB tracing demo images" \
    --project="${PROJECT_ID}" \
    --quiet
fi

# One immutable tag per build. A fixed tag would leave the Deployment spec unchanged (no rollout) and nodes would
# reuse the cached image, so a rebuild would never reach the pods. SKIP_BUILD=1 redeploys an existing IMAGE_TAG.
if [ "${SKIP_BUILD:-0}" = "1" ]; then
  : "${IMAGE_TAG:?SKIP_BUILD=1 needs IMAGE_TAG set to an already-built tag}"
else
  IMAGE_TAG="${IMAGE_TAG:-$(date -u +%Y%m%dT%H%M%SZ)}"
fi
echo "==> Image tag: ${IMAGE_TAG}"
AGENT_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/p1-lb-trace-repo/storefront-agent:${IMAGE_TAG}"
CHECKOUT_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/p1-lb-trace-repo/checkout-api:${IMAGE_TAG}"
BUILD_SA="projects/${PROJECT_ID}/serviceAccounts/p1-node-build-gsa@${PROJECT_ID}.iam.gserviceaccount.com"

if [ "${SKIP_BUILD:-0}" != "1" ]; then
  echo "==> Building container images in Cloud Build..."
  gcloud builds submit "${ROOT_DIR}/app/agent" \
    --tag="${AGENT_IMAGE}" \
    --region="${REGION}" \
    --service-account="${BUILD_SA}" \
    --default-buckets-behavior=regional-user-owned-bucket \
    --project="${PROJECT_ID}" \
    --quiet

  gcloud builds submit "${ROOT_DIR}/app/checkout_api" \
    --tag="${CHECKOUT_IMAGE}" \
    --region="${REGION}" \
    --service-account="${BUILD_SA}" \
    --default-buckets-behavior=regional-user-owned-bucket \
    --project="${PROJECT_ID}" \
    --quiet
fi

echo "==> Applying Namespace, KSA, and Managed OTel Instrumentation CRs..."
sed -e "s|PROJECT_ID|${PROJECT_ID}|g" -e "s|REGION|${REGION}|g" \
  "${ROOT_DIR}/k8s/00-namespace-sa.yaml" | kubectl apply -f -

echo "==> Applying Tier 2 checkout-api + gke-l7-rilb Gateway..."
sed -e "s|PROJECT_ID|${PROJECT_ID}|g" -e "s|REGION|${REGION}|g" -e "s|IMAGE_TAG|${IMAGE_TAG}|g" \
  "${ROOT_DIR}/k8s/10-checkout-api-rilb.yaml" | kubectl apply -f -

kubectl rollout status deployment/checkout-api -n p1-demo --timeout=300s
if ! kubectl exec deploy/checkout-api -n p1-demo -- env | grep -q '^OTEL_EXPORTER_OTLP_ENDPOINT='; then
  kubectl rollout restart deployment/checkout-api -n p1-demo
  kubectl rollout status deployment/checkout-api -n p1-demo --timeout=300s
fi
kubectl exec deploy/checkout-api -n p1-demo -- env | grep '^OTEL_' | sort

kubectl wait --for=condition=Programmed gateway/p1-checkout-rilb-gw -n p1-demo --timeout=600s
RILB_VIP="$(kubectl get gateway p1-checkout-rilb-gw -n p1-demo -o jsonpath='{.status.addresses[0].value}')"
echo "RILB_VIP=${RILB_VIP}"

echo "==> Applying Tier 1 storefront-agent + gke-l7-global-external-managed Gateway..."
sed -e "s|PROJECT_ID|${PROJECT_ID}|g" \
    -e "s|REGION|${REGION}|g" \
    -e "s|RILB_VIP|${RILB_VIP}|g" \
    -e "s|IMAGE_TAG|${IMAGE_TAG}|g" \
    "${ROOT_DIR}/k8s/20-storefront-agent-gw.yaml" | kubectl apply -f -

kubectl rollout status deployment/storefront-agent -n p1-demo --timeout=300s
if ! kubectl exec deploy/storefront-agent -n p1-demo -- env | grep -q '^OTEL_EXPORTER_OTLP_ENDPOINT='; then
  kubectl rollout restart deployment/storefront-agent -n p1-demo
  kubectl rollout status deployment/storefront-agent -n p1-demo --timeout=300s
fi
kubectl exec deploy/storefront-agent -n p1-demo -- env | grep '^OTEL_' | sort

kubectl wait --for=condition=Programmed gateway/p1-storefront-ext-gw -n p1-demo --timeout=600s
EXT_VIP="$(kubectl get gateway p1-storefront-ext-gw -n p1-demo -o jsonpath='{.status.addresses[0].value}')"
echo "EXT_VIP=${EXT_VIP}"

echo "==> Waiting for Global External Gateway GFE + Cloud Armor WAF propagation..."
for _ in $(seq 1 45); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H 'x-demo-attack: true' "http://${EXT_VIP}/order" || true)"
  if [ "${code}" = "403" ]; then
    echo "Global External Gateway + Cloud Armor ready (HTTP 403 verified on attack header)."
    break
  fi
  echo "  current status=${code:-timeout}, retrying in 10s..."
  sleep 10
done

echo "==> Phase 2 complete: EXT_VIP=${EXT_VIP} (ADK Web UI: http://${EXT_VIP}/), RILB_VIP=${RILB_VIP}, IMAGE_TAG=${IMAGE_TAG}"
