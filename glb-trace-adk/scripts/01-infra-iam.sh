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

echo "==> [Phase 1] Target project=${PROJECT_ID} (${ZONE})"
gcloud config set project "${PROJECT_ID}" --quiet
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
export PROJECT_NUMBER

echo "==> Enabling required Google Cloud APIs..."
gcloud services enable \
  compute.googleapis.com \
  container.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  networksecurity.googleapis.com \
  networkservices.googleapis.com \
  cloudtrace.googleapis.com \
  telemetry.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  aiplatform.googleapis.com \
  storage.googleapis.com \
  --project="${PROJECT_ID}" \
  --quiet

echo "==> Provisioning Compute Engine, Network Security, and GKE service agents..."
for svc in compute.googleapis.com networksecurity.googleapis.com container.googleapis.com; do
  gcloud beta services identity create --service="${svc}" --project="${PROJECT_ID}" --quiet || true
done

echo "==> Granting Cloud Trace & Telemetry write roles to LB service agents..."
for sa in \
  "service-${PROJECT_NUMBER}@compute-system.iam.gserviceaccount.com" \
  "service-${PROJECT_NUMBER}@gcp-sa-networksecurity.iam.gserviceaccount.com"; do
  for trace_role in roles/cloudtrace.agent roles/telemetry.tracesWriter; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${sa}" \
      --role="${trace_role}" \
      --condition=None \
      --quiet >/dev/null
  done
done

echo "==> Creating least-privilege GKE Node + Cloud Build SA (p1-node-build-gsa)..."
NODE_SA_EMAIL="p1-node-build-gsa@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "${NODE_SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create p1-node-build-gsa \
    --display-name="GKE LB tracing demo: node and Cloud Build SA" \
    --project="${PROJECT_ID}" \
    --quiet
fi

for role in \
  roles/container.defaultNodeServiceAccount \
  roles/artifactregistry.writer \
  roles/telemetry.tracesWriter \
  roles/cloudtrace.agent \
  roles/logging.logWriter \
  roles/monitoring.metricWriter \
  roles/storage.admin; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${NODE_SA_EMAIL}" \
    --role="${role}" \
    --condition=None \
    --quiet >/dev/null
done

echo "==> Creating Workload Identity GSA for pods (p1-otel-trace-gsa)..."
APP_SA_EMAIL="p1-otel-trace-gsa@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "${APP_SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create p1-otel-trace-gsa \
    --display-name="GKE LB tracing demo: workload identity SA" \
    --project="${PROJECT_ID}" \
    --quiet
fi

for role in \
  roles/aiplatform.user \
  roles/telemetry.tracesWriter \
  roles/cloudtrace.agent \
  roles/logging.logWriter \
  roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${APP_SA_EMAIL}" \
    --role="${role}" \
    --condition=None \
    --quiet >/dev/null
done

echo "==> Creating custom VPC (${NETWORK_NAME}), subnets, firewall, and Cloud Armor WAF..."
if ! gcloud compute networks describe "${NETWORK_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute networks create "${NETWORK_NAME}" \
    --subnet-mode=custom \
    --project="${PROJECT_ID}" \
    --quiet
fi

if ! gcloud compute networks subnets describe p1-gke-subnet --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute networks subnets create p1-gke-subnet \
    --network="${NETWORK_NAME}" \
    --region="${REGION}" \
    --range="10.128.0.0/20" \
    --enable-private-ip-google-access \
    --project="${PROJECT_ID}" \
    --quiet
fi

if ! gcloud compute networks subnets describe p1-regional-proxy-subnet --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute networks subnets create p1-regional-proxy-subnet \
    --purpose=REGIONAL_MANAGED_PROXY \
    --role=ACTIVE \
    --network="${NETWORK_NAME}" \
    --region="${REGION}" \
    --range="10.129.0.0/23" \
    --project="${PROJECT_ID}" \
    --quiet
fi

if ! gcloud compute firewall-rules describe p1-allow-alb-and-hc --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute firewall-rules create p1-allow-alb-and-hc \
    --network="${NETWORK_NAME}" \
    --action=ALLOW \
    --direction=INGRESS \
    --rules=tcp:8080 \
    --source-ranges="130.211.0.0/22,35.191.0.0/16,10.129.0.0/23" \
    --project="${PROJECT_ID}" \
    --quiet
fi

if ! gcloud compute security-policies describe p1-storefront-waf --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute security-policies create p1-storefront-waf \
    --description="GKE LB tracing demo WAF for edge-deny tracing" \
    --project="${PROJECT_ID}" \
    --quiet
  gcloud compute security-policies rules create 1000 \
    --security-policy=p1-storefront-waf \
    --expression="request.headers['x-demo-attack'] == 'true'" \
    --action=deny-403 \
    --project="${PROJECT_ID}" \
    --quiet
fi

RAPID_VERSION="$(gcloud container get-server-config \
  --zone="${ZONE}" \
  --project="${PROJECT_ID}" \
  --format='json(channels)' | python3 -c '
import json, sys
channels = json.load(sys.stdin).get("channels", [])
rapid = next(c for c in channels if c.get("channel") == "RAPID")
print(rapid["validVersions"][0])
')"

echo "==> Creating minimum-footprint zonal GKE cluster (${CLUSTER_NAME}, RAPID ${RAPID_VERSION}, Managed OTel GA, 1x e2-standard-4)..."
if ! gcloud container clusters describe "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud container clusters create "${CLUSTER_NAME}" \
    --zone="${ZONE}" \
    --release-channel=rapid \
    --cluster-version="${RAPID_VERSION}" \
    --managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS \
    --num-nodes=1 \
    --machine-type=e2-standard-4 \
    --disk-type=pd-balanced \
    --disk-size=30 \
    --network="${NETWORK_NAME}" \
    --subnetwork=p1-gke-subnet \
    --enable-ip-alias \
    --enable-private-nodes \
    --enable-dns-access \
    --no-enable-ip-access \
    --shielded-secure-boot \
    --shielded-integrity-monitoring \
    --gateway-api=standard \
    --workload-pool="${PROJECT_ID}.svc.id.goog" \
    --service-account="${NODE_SA_EMAIL}" \
    --addons=HttpLoadBalancing \
    --project="${PROJECT_ID}" \
    --quiet
fi

echo "==> Binding Workload Identity (${PROJECT_ID}.svc.id.goog[p1-demo/p1-otel-app-ksa])..."
gcloud iam service-accounts add-iam-policy-binding "${APP_SA_EMAIL}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="serviceAccount:${PROJECT_ID}.svc.id.goog[p1-demo/p1-otel-app-ksa]" \
  --project="${PROJECT_ID}" \
  --quiet >/dev/null

echo "==> Phase 1 complete (${ROOT_DIR})."
