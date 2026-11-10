"""Tracing wiring: health checks stay silent, business endpoints emit one server span and trace-correlated logs."""

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from checkout_api.structured_logging import CloudLoggingJsonFormatter

PROJECT_ID = "test-project"  # matches conftest's default for GOOGLE_CLOUD_PROJECT


def _server_spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return [span for span in exporter.get_finished_spans() if span.kind == SpanKind.SERVER]


def _status_code(span: ReadableSpan) -> int:
    """Status code under either HTTP semantic-convention generation (OTEL_SEMCONV_STABILITY_OPT_IN)."""
    attributes = span.attributes or {}
    return attributes.get("http.response.status_code", attributes.get("http.status_code"))


def test_healthz_is_excluded_from_tracing(client: TestClient, span_exporter: InMemorySpanExporter) -> None:
    assert client.get("/healthz").status_code == 200
    assert span_exporter.get_finished_spans() == ()


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "route"),
    [
        ("GET", "/carts/cart-1001", None, 200, "/carts/{cart_id}"),
        ("GET", "/carts/nope", None, 404, "/carts/{cart_id}"),
        ("POST", "/orders", {"cart_id": "cart-1001", "zip_code": "94043"}, 200, "/orders"),
    ],
)
def test_endpoints_emit_exactly_one_server_span(
    client: TestClient,
    span_exporter: InMemorySpanExporter,
    method: str,
    path: str,
    body: dict[str, object] | None,
    status: int,
    route: str,
) -> None:
    response = client.request(method, path, json=body)

    assert response.status_code == status
    server_spans = _server_spans(span_exporter)
    assert len(server_spans) == 1
    assert server_spans[0].attributes["http.route"] == route
    assert _status_code(server_spans[0]) == status


def test_inbound_traceparent_becomes_the_parent_of_the_server_span(
    client: TestClient, span_exporter: InMemorySpanExporter
) -> None:
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    client.get("/carts/cart-1002", headers={"traceparent": f"00-{trace_id}-00f067aa0ba902b7-01"})

    (server_span,) = _server_spans(span_exporter)
    assert f"{server_span.context.trace_id:032x}" == trace_id
    assert f"{server_span.parent.span_id:016x}" == "00f067aa0ba902b7"


def test_order_log_line_is_correlated_with_the_request_trace(
    client: TestClient, span_exporter: InMemorySpanExporter
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(CloudLoggingJsonFormatter(PROJECT_ID))
    app_logger = logging.getLogger("checkout_api.main")
    app_logger.addHandler(handler)
    try:
        response = client.post("/orders", json={"cart_id": "cart-1001", "zip_code": "94043", "delay_ms": 0})
    finally:
        app_logger.removeHandler(handler)

    assert response.status_code == 200
    (server_span,) = _server_spans(span_exporter)
    payload = json.loads(stream.getvalue())
    assert payload["message"].startswith(f"placed order={response.json()['order_id']}")
    assert (
        payload["logging.googleapis.com/trace"] == f"projects/{PROJECT_ID}/traces/{server_span.context.trace_id:032x}"
    )
    assert payload["logging.googleapis.com/trace_sampled"] is True
