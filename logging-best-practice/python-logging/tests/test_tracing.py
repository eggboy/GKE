"""Query-string values on the server span."""

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from python_logging.tracing import redact_query, redact_query_on_span

CANARY = "review-canary"


def _server_span_attributes(exporter: InMemorySpanExporter) -> dict[str, object]:
    (server,) = (span for span in exporter.get_finished_spans() if span.kind is SpanKind.SERVER)
    return dict(server.attributes or {})


def _all_attribute_text(exporter: InMemorySpanExporter) -> str:
    return " ".join(str(dict(span.attributes or {})) for span in exporter.get_finished_spans())


@pytest.mark.parametrize(
    ("query", "redacted"),
    [
        ("cartId=cart-1001&access_token=review-canary", "cartId=REDACTED&access_token=REDACTED"),
        ("a=", "a=REDACTED"),
        ("a=1=2", "a=REDACTED"),
        ("signed-token-without-a-name", "REDACTED"),
        ("a=1&&b=2", "a=REDACTED&&b=REDACTED"),
    ],
)
def test_query_keeps_parameter_names_and_loses_their_values(query: str, redacted: str) -> None:
    assert redact_query(query) == redacted


def test_placed_order_leaves_no_query_value_on_any_span(
    client: TestClient, finished_spans: InMemorySpanExporter
) -> None:
    response = client.post("/orders", params={"cartId": "cart-1001", "access_token": CANARY})

    assert response.status_code == 200
    assert _server_span_attributes(finished_spans)["http.url"].endswith("/orders?cartId=REDACTED&access_token=REDACTED")
    assert CANARY not in _all_attribute_text(finished_spans)
    assert "cart-1001" not in _all_attribute_text(finished_spans)


def test_request_that_matches_no_route_is_redacted_as_well(
    client: TestClient, finished_spans: InMemorySpanExporter
) -> None:
    assert client.get("/nope", params={"access_token": CANARY}).status_code == 404

    assert _server_span_attributes(finished_spans)["http.url"].endswith("/nope?access_token=REDACTED")
    assert CANARY not in _all_attribute_text(finished_spans)


def test_request_without_a_query_keeps_its_url(client: TestClient, finished_spans: InMemorySpanExporter) -> None:
    assert client.get("/boom").status_code == 500

    assert _server_span_attributes(finished_spans)["http.url"].endswith("/boom")


def test_attributes_of_the_stable_http_conventions_are_redacted() -> None:
    exporter = InMemorySpanExporter()
    tracer = TracerProvider().get_tracer("tests")
    span = tracer.start_span(
        "POST /orders",
        attributes={
            "url.path": "/orders",
            "url.query": f"cartId=cart-1001&access_token={CANARY}",
            "url.full": f"https://shop.example/orders?access_token={CANARY}",
            "http.target": f"/orders?access_token={CANARY}",
        },
    )
    redact_query_on_span(span)
    span.end()
    exporter.export([span])
    attributes = dict(exporter.get_finished_spans()[0].attributes or {})

    assert attributes["url.query"] == "cartId=REDACTED&access_token=REDACTED"
    assert attributes["url.full"] == "https://shop.example/orders?access_token=REDACTED"
    assert attributes["http.target"] == "/orders?access_token=REDACTED"
    assert attributes["url.path"] == "/orders"


def test_span_that_does_not_record_is_left_alone() -> None:
    redact_query_on_span(trace.INVALID_SPAN)  # the request was excluded from tracing: nothing to rewrite, no error
