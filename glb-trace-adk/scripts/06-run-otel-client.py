# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx>=0.27,<1",
#     "opentelemetry-sdk>=1.39,<2",
#     "opentelemetry-exporter-otlp-proto-http>=1.39,<2",
#     "opentelemetry-instrumentation-httpx>=0.54b0,<1",
#     "python-dotenv>=1.0.1,<2",
# ]
# ///
"""Scenario H: an OpenTelemetry-instrumented client calling the agent through the External ALB.

The client exports its own span to Cloud Trace (OTLP over HTTP to telemetry.googleapis.com), then calls
http://EXT_VIP/order with httpx. The External ALB reads the client's traceparent and parents its span under the
client span, so Trace Explorer shows: demo-client -> External ALB -> storefront-agent -> Internal ALB -> checkout-api,
with no "Missing span ID" placeholder.

Run from the repository root (needs gcloud auth as a user with roles/telemetry.tracesWriter or owner):

    uv run scripts/06-run-otel-client.py [--ext-vip 34.54.17.196] [--delay-ms 0]
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

ROOT_DIR = Path(__file__).resolve().parent.parent
OTLP_TRACES_ENDPOINT = "https://telemetry.googleapis.com/v1/traces"
CLOUD_TRACE_API = "https://cloudtrace.googleapis.com/v1"
SERVICE_NAME = "demo-client"
CLIENT_SPAN_NAME = "demo-client checkout"
VERIFY_TIMEOUT_S = 90
POLL_INTERVAL_S = 5

Span = dict[str, Any]


def gcloud_access_token(project_id: str) -> str:
    """Return an access token for the active gcloud account."""
    return subprocess.check_output(
        ["gcloud", "auth", "print-access-token", f"--project={project_id}"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def discover_ext_vip() -> str:
    """Read the External Gateway VIP from the cluster."""
    env = {**os.environ, "CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS": "false"}
    return subprocess.check_output(
        [
            "kubectl",
            "get",
            "gateway",
            "p1-storefront-ext-gw",
            "-n",
            "p1-demo",
            "-o",
            "jsonpath={.status.addresses[0].value}",
        ],
        text=True,
        env=env,
    ).strip()


def configure_tracing(project_id: str, token: str) -> TracerProvider:
    """Create a TracerProvider exporting OTLP/HTTP spans to Cloud Trace."""
    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.namespace": "p1-demo",
            "gcp.project_id": project_id,
        }
    )
    exporter = OTLPSpanExporter(
        endpoint=OTLP_TRACES_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "x-goog-user-project": project_id,
        },
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    return provider


def trace_id_from_traceresponse(header: str | None) -> str | None:
    """Trace ID from a W3C ``traceresponse`` header (``00-<trace-id>-<span-id>-<flags>``), or None when absent."""
    if not header:
        return None
    fields = header.split("-")
    return fields[1] if len(fields) == 4 else None


def fetch_trace(api: httpx.Client, project_id: str, trace_id: str) -> list[Span] | None:
    """Return spans for trace_id from the Cloud Trace v1 API, or None if it is not ingested yet."""
    response = api.get(f"{CLOUD_TRACE_API}/projects/{project_id}/traces/{trace_id}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json().get("spans", [])


def is_external_alb_span(span: Span) -> bool:
    """True for the span written by the Global External ALB forwarding rule."""
    return "/global/forwardingRules/" in span.get("labels", {}).get("cloud.resource.id", "")


def is_client_span(span: Span) -> bool:
    """True for the root span this script exported."""
    return span.get("name") == CLIENT_SPAN_NAME


def wait_for_trace(api: httpx.Client, project_id: str, trace_id: str) -> list[Span]:
    """Poll Cloud Trace until both the client span and the External ALB span are visible, or the deadline passes.

    The client span is flushed synchronously and lands first; the ALB and pod spans arrive a few seconds later
    (SDK batch delay plus the managed collector). Checking parentage against a partial trace gives false failures.
    """
    deadline = time.monotonic() + VERIFY_TIMEOUT_S
    spans: list[Span] = []
    while time.monotonic() < deadline:
        spans = fetch_trace(api, project_id, trace_id) or []
        if any(is_client_span(s) for s in spans) and any(is_external_alb_span(s) for s in spans):
            break
        time.sleep(POLL_INTERVAL_S)
    return spans


def report_parentage(spans: list[Span]) -> bool:
    """Print the client and External ALB spans and return True when the ALB span hangs under the client span."""
    by_id = {s["spanId"]: s for s in spans}
    client_root = next((s for s in spans if is_client_span(s)), None)
    lb_span = next((s for s in spans if is_external_alb_span(s)), None)
    print(f"Spans in Cloud Trace   : {len(spans)}")
    if client_root:
        print(f"Client span            : {client_root['name']!r} spanId={client_root['spanId']} parent=ROOT")
    parent: Span = {}
    if lb_span:
        parent = by_id.get(lb_span.get("parentSpanId", ""), {})
        print(
            f"External ALB span      : {lb_span['name']!r} parent={lb_span.get('parentSpanId')} "
            f"({parent.get('name', 'unknown')!r}, service={parent.get('labels', {}).get('service.name')!r})"
        )
    return (
        client_root is not None and lb_span is not None and parent.get("labels", {}).get("service.name") == SERVICE_NAME
    )


def parse_args() -> argparse.Namespace:
    """Parse CLI flags; PROJECT_ID comes from the flag, the environment or the repository's .env, in that order."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    project_id = os.environ.get("PROJECT_ID")
    parser.add_argument("--project-id", default=project_id, required=project_id is None)
    parser.add_argument("--ext-vip", default=os.environ.get("EXT_VIP") or None)
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--prompt", default="Check out cart cart-1001 and ship it to ZIP 94043.")
    return parser.parse_args()


def main() -> int:
    """Run one traced client request and verify the client span is the root in Cloud Trace."""
    load_dotenv(ROOT_DIR / ".env", override=True)
    args = parse_args()

    ext_vip = args.ext_vip or discover_ext_vip()
    token = gcloud_access_token(args.project_id)
    provider = configure_tracing(args.project_id, token)
    tracer = trace.get_tracer("p1.demo_client")

    with tracer.start_as_current_span(CLIENT_SPAN_NAME, kind=trace.SpanKind.CLIENT) as span:
        ctx = span.get_span_context()
        trace_id = f"{ctx.trace_id:032x}"
        client_span_id = f"{ctx.span_id:016x}"
        span.set_attribute("demo.scenario", "H")
        span.set_attribute("demo.target", f"http://{ext_vip}/order")
        with httpx.Client(timeout=90.0) as client:
            response = client.get(f"http://{ext_vip}/order", params={"prompt": args.prompt, "delay_ms": args.delay_ms})
        span.set_attribute("demo.http_status", response.status_code)
        if response.status_code != 200:
            span.set_status(trace.StatusCode.ERROR, f"HTTP {response.status_code}")
        else:
            span.set_attribute("demo.agent_reply", response.json().get("agent_reply", "")[:200])

    provider.force_flush(timeout_millis=10_000)
    provider.shutdown()

    print("==================================================================")
    print("Scenario H: OTel client -> External ALB -> ADK agent -> Internal ALB -> checkout-api")
    print("==================================================================")
    print(f"HTTP {response.status_code}")
    print(f"Client trace id        : {trace_id}")
    print(f"Client root span id    : {client_span_id}")
    if response.status_code != 200:
        print(f"FAIL: /order returned HTTP {response.status_code}: {response.text[:300]}", file=sys.stderr)
        return 1
    traceresponse = response.headers.get("traceresponse")
    pod_trace_id = trace_id_from_traceresponse(traceresponse)
    print(f"traceresponse from pod : {traceresponse}")
    print(f"Reply                  : {response.json().get('agent_reply')}")
    if pod_trace_id != trace_id:
        print(f"FAIL: pod answered with trace {pod_trace_id}, client sent {trace_id}", file=sys.stderr)
        return 1

    print("Waiting for Cloud Trace ingestion to verify span parentage...")
    with httpx.Client(timeout=15.0, headers={"Authorization": f"Bearer {token}"}) as api:
        spans = wait_for_trace(api, args.project_id, trace_id)

    if not spans:
        print("WARN: trace not visible in Cloud Trace API yet; open Trace Explorer in a minute.")
    elif report_parentage(spans):
        print("PASS: External ALB span is parented under the demo-client span tree")
    else:
        print("CHECK: parentage not confirmed (see spans above)")

    print(f"Trace Explorer: https://console.cloud.google.com/traces/list?project={args.project_id}&tid={trace_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
