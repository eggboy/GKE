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
export NETWORK_NAME="${NETWORK_NAME:-p1-lb-trace-vpc}"
export CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS=false
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"

echo "==> [Cleanup 1/5] Deleting TelemetryPolicy resources..."
gcloud beta network-services telemetry-policies delete p1-checkout-tracing-policy \
  --location="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true
gcloud beta network-services telemetry-policies delete p1-storefront-tracing-policy \
  --location=global --project="${PROJECT_ID}" --quiet 2>/dev/null || true

echo "==> [Cleanup 2/5] Deleting GKE Gateways & Workloads so controllers release LBs and WAF..."
if gcloud container clusters describe "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone="${ZONE}" --dns-endpoint --project="${PROJECT_ID}" --quiet || true
  EXT_VIP="$(kubectl get gateway p1-storefront-ext-gw -n p1-demo -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)"

  kubectl delete -f "${ROOT_DIR}/k8s/20-storefront-agent-gw.yaml" --ignore-not-found || true
  kubectl delete -f "${ROOT_DIR}/k8s/10-checkout-api-rilb.yaml" --ignore-not-found || true
  kubectl delete -f "${ROOT_DIR}/k8s/00-namespace-sa.yaml" --ignore-not-found || true

  if [ -n "${EXT_VIP:-}" ]; then
    for _ in $(seq 1 36); do
      if [ -z "$(gcloud compute forwarding-rules list --global --filter="IPAddress=${EXT_VIP}" --format='value(name)' --project="${PROJECT_ID}")" ]; then
        break
      fi
      echo "  waiting for gke-l7-global-external-managed forwarding rule cleanup..."
      sleep 10
    done
  fi

  for _ in $(seq 1 36); do
    if [ -z "$(gcloud compute forwarding-rules list --regions="${REGION}" --filter="network~/${NETWORK_NAME}$" --format='value(name)' --project="${PROJECT_ID}")" ]; then
      break
    fi
    echo "  waiting for gke-l7-rilb forwarding rule cleanup..."
    sleep 10
  done

  echo "==> [Cleanup 3/5] Deleting GKE cluster ${CLUSTER_NAME}..."
  gcloud container clusters delete "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}" --quiet
fi

echo "==> [Cleanup 4/5] Deleting Cloud Armor WAF, Firewall, Subnets, and VPC..."
gcloud compute security-policies delete p1-storefront-waf --project="${PROJECT_ID}" --quiet 2>/dev/null || true
gcloud compute firewall-rules delete p1-allow-alb-and-hc --project="${PROJECT_ID}" --quiet 2>/dev/null || true
gcloud compute networks subnets delete p1-regional-proxy-subnet --region="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true
gcloud compute networks subnets delete p1-gke-subnet --region="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true
gcloud compute networks delete "${NETWORK_NAME}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true

echo "==> [Cleanup 5/5] Deleting Artifact Registry, Cloud Build bucket, IAM bindings, and GSAs..."
gcloud artifacts repositories delete p1-lb-trace-repo --location="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true
gcloud storage rm --recursive "gs://${PROJECT_ID}_${REGION}_cloudbuild" --project="${PROJECT_ID}" --quiet 2>/dev/null || true

APP_SA_EMAIL="p1-otel-trace-gsa@${PROJECT_ID}.iam.gserviceaccount.com"
NODE_SA_EMAIL="p1-node-build-gsa@${PROJECT_ID}.iam.gserviceaccount.com"

for role in roles/aiplatform.user roles/telemetry.tracesWriter roles/cloudtrace.agent roles/logging.logWriter roles/monitoring.metricWriter; do
  gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${APP_SA_EMAIL}" --role="${role}" --all --quiet >/dev/null 2>&1 || true
done
for role in roles/container.defaultNodeServiceAccount roles/artifactregistry.writer roles/telemetry.tracesWriter roles/cloudtrace.agent roles/logging.logWriter roles/monitoring.metricWriter roles/storage.admin; do
  gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${NODE_SA_EMAIL}" --role="${role}" --all --quiet >/dev/null 2>&1 || true
done

gcloud iam service-accounts delete "${APP_SA_EMAIL}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true
gcloud iam service-accounts delete "${NODE_SA_EMAIL}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true

echo "==> GKE LB tracing demo cleanup complete for ${PROJECT_ID} (projectNumber=${PROJECT_NUMBER})."
