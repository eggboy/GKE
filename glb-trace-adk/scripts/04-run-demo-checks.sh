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

# Trace ID from the W3C traceresponse header (00-<trace-id>-<span-id>-<flags>) that the agent's OpenTelemetry ASGI
# middleware adds to every traced response. $1 is a header dump written by `curl -D`.
trace_id_from_headers() {
  local trace_id
  trace_id="$(tr -d '\r' < "$1" | awk 'tolower($1) == "traceresponse:" { split($2, f, "-"); print f[2]; exit }')"
  if [ -z "${trace_id}" ]; then
    echo "FAIL: no traceresponse header in $1 (is the agent's OpenTelemetry middleware active?)" >&2
    return 1
  fi
  echo "${trace_id}"
}

EXT_VIP="$(kubectl get gateway p1-storefront-ext-gw -n p1-demo -o jsonpath='{.status.addresses[0].value}')"
EXT_FR_NAME="$(gcloud compute forwarding-rules list \
  --global \
  --filter="IPAddress=${EXT_VIP} AND loadBalancingScheme=EXTERNAL_MANAGED" \
  --format='value(name)' \
  --project="${PROJECT_ID}")"
RILB_VIP="$(kubectl get gateway p1-checkout-rilb-gw -n p1-demo -o jsonpath='{.status.addresses[0].value}')"

echo "=================================================================="
echo "Scenario A: Full 6-hop GKE Gateway + ADK Agent + Internal ALB Trace"
echo "=================================================================="
curl -sS -D /tmp/p1-headers-a.txt -o /tmp/p1-body-a.json -w "HTTP %{http_code}\n" "http://${EXT_VIP}/order"
TRACE_ID="$(trace_id_from_headers /tmp/p1-headers-a.txt)"
python3 -c 'import json; b=json.load(open("/tmp/p1-body-a.json")); print("Reply:", b["agent_reply"]); print("Tools:", b["tool_calls"])'
echo "Trace ID (traceresponse header): ${TRACE_ID}"
echo "Trace Explorer: https://console.cloud.google.com/traces/list?project=${PROJECT_ID}&tid=${TRACE_ID}"

echo ""
echo "=================================================================="
echo "Scenario B: Upstream W3C traceparent Continuity"
echo "=================================================================="
CLIENT_TRACE_ID="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
curl -sS -D /tmp/p1-headers-b.txt -o /dev/null -w "HTTP %{http_code}\n" \
  -H "traceparent: 00-${CLIENT_TRACE_ID}-00f067aa0ba902b7-01" \
  "http://${EXT_VIP}/order"
POD_TRACE_ID="$(trace_id_from_headers /tmp/p1-headers-b.txt)"
if [ "${POD_TRACE_ID}" != "${CLIENT_TRACE_ID}" ]; then
  echo "FAIL: pod answered with trace ${POD_TRACE_ID}, client sent ${CLIENT_TRACE_ID}" >&2
  exit 1
fi
echo "Verified continuity: ${POD_TRACE_ID}"
echo "Trace Explorer: https://console.cloud.google.com/traces/list?project=${PROJECT_ID}&tid=${CLIENT_TRACE_ID}"

echo ""
echo "=================================================================="
echo "Scenario C: Edge-Only Zero-Pod Span (/edge-404 on External Gateway)"
echo "=================================================================="
EDGE_TRACE_ID="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  -H "traceparent: 00-${EDGE_TRACE_ID}-00f067aa0ba902b7-01" \
  "http://${EXT_VIP}/edge-404"
echo "Edge 404 Trace Explorer: https://console.cloud.google.com/traces/list?project=${PROJECT_ID}&tid=${EDGE_TRACE_ID}"

echo ""
echo "=================================================================="
echo "Scenario D: Internal parentBasedSampling (sampled=1 vs sampled=0)"
echo "=================================================================="
TRACE_A="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
TRACE_B="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
kubectl exec deploy/storefront-agent -n p1-demo -- python -c "
import httpx
r1 = httpx.get('http://${RILB_VIP}/carts/cart-1001', headers={'traceparent': '00-${TRACE_A}-1111111111111111-01'})
r0 = httpx.get('http://${RILB_VIP}/carts/cart-1001', headers={'traceparent': '00-${TRACE_B}-2222222222222222-00'})
print('sampled=1:', r1.status_code, r1.text)
print('sampled=0:', r0.status_code, r0.text)
"
echo "Sampled Trace A:   https://console.cloud.google.com/traces/list?project=${PROJECT_ID}&tid=${TRACE_A}"
echo "Unsampled Trace B: https://console.cloud.google.com/traces/list?project=${PROJECT_ID}&tid=${TRACE_B}"

echo ""
echo "=================================================================="
echo "Scenario F: Cloud Armor WAF 403 Edge-Deny Trace"
echo "=================================================================="
WAF_TRACE_ID="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  -H "x-demo-attack: true" \
  -H "traceparent: 00-${WAF_TRACE_ID}-3333333333333333-01" \
  "http://${EXT_VIP}/order"
echo "WAF 403 Trace Explorer: https://console.cloud.google.com/traces/list?project=${PROJECT_ID}&tid=${WAF_TRACE_ID}"

echo ""
echo "=================================================================="
echo "Scenario G: Backend Outage (503) & Latency Attribution (1500 ms)"
echo "=================================================================="
kubectl scale deployment/checkout-api -n p1-demo --replicas=0
kubectl wait --for=delete pod -l app=checkout-api -n p1-demo --timeout=120s
curl -sS -D /tmp/p1-headers-outage.txt -o /dev/null -w "HTTP %{http_code}\n" "http://${EXT_VIP}/order"
OUTAGE_TRACE_ID="$(trace_id_from_headers /tmp/p1-headers-outage.txt)"
echo "Outage Trace Explorer: https://console.cloud.google.com/traces/list?project=${PROJECT_ID}&tid=${OUTAGE_TRACE_ID}"

kubectl scale deployment/checkout-api -n p1-demo --replicas=1
kubectl rollout status deployment/checkout-api -n p1-demo --timeout=300s
until kubectl exec deploy/storefront-agent -n p1-demo -- python -c "import httpx,sys; sys.exit(0 if httpx.get('http://${RILB_VIP}/carts/cart-1001').status_code==200 else 1)" >/dev/null 2>&1; do
  sleep 5
done

curl -sS -D /tmp/p1-headers-latency.txt -o /dev/null -w "HTTP %{http_code}\n" "http://${EXT_VIP}/order?delay_ms=1500"
LATENCY_TRACE_ID="$(trace_id_from_headers /tmp/p1-headers-latency.txt)"
echo "Latency (1500 ms) Trace Explorer: https://console.cloud.google.com/traces/list?project=${PROJECT_ID}&tid=${LATENCY_TRACE_ID}"

echo ""
echo "=================================================================="
echo "Scenario E: Unified Cloud Logging Correlation for Scenario A"
echo "=================================================================="
gcloud logging read "trace=\"projects/${PROJECT_ID}/traces/${TRACE_ID}\"" \
  --project="${PROJECT_ID}" \
  --limit=20 \
  --format="table(timestamp, resource.type, httpRequest.status, jsonPayload.message)"

echo ""
echo "=================================================================="
echo "Scenario F (cont.): Cloud Armor DENY log joined to the 403 edge span"
echo "=================================================================="
echo "Note: LB spans follow OTel HTTP server semantics, so 4xx (403/404) spans keep status OK."
echo "Find them in Trace Explorer with filter: http.response.status_code = 403"
gcloud logging read "trace=\"projects/${PROJECT_ID}/traces/${WAF_TRACE_ID}\"" \
  --project="${PROJECT_ID}" \
  --limit=5 \
  --format="table(timestamp, httpRequest.status, jsonPayload.statusDetails, jsonPayload.enforcedSecurityPolicy.name, jsonPayload.enforcedSecurityPolicy.outcome, spanId)"
