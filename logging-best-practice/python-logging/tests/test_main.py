"""Status codes and log lines of the sample application."""

import io
import json
import logging
from typing import Any

from fastapi.testclient import TestClient

from python_logging.structured_logging import SOURCE_LOCATION, SPAN_ID, TRACE, TRACE_SAMPLED
from tests.support import PROJECT_ID, parse_line


def _lines(buffer: io.StringIO) -> list[dict[str, Any]]:
    return [parse_line(line + "\n") for line in buffer.getvalue().splitlines()]


def test_placed_order_is_one_info_line_with_its_fields_and_trace(client: TestClient, stdout_lines: io.StringIO) -> None:
    response = client.post(
        "/orders",
        params={"cartId": "cart-1001"},
        headers={"traceparent": "00-7d1f0e5a9c3b4a6e8f2d1c0b9a8e7f81-1111111111111111-01"},
    )

    assert response.status_code == 200
    (line,) = _lines(stdout_lines)
    assert line["severity"] == "INFO"
    assert line["message"] == "placed order"
    assert line["logger"] == "python_logging.main"
    assert line["cart_id"] == "cart-1001"
    assert line["total"] == 67.97
    assert line["order_id"] == response.json()["order_id"]
    assert line[SOURCE_LOCATION]["function"] == "place_order"
    assert line[TRACE] == f"projects/{PROJECT_ID}/traces/7d1f0e5a9c3b4a6e8f2d1c0b9a8e7f81"
    assert len(line[SPAN_ID]) == 16
    assert line[TRACE_SAMPLED] is True


def test_unknown_cart_is_404_with_one_warning_that_carries_the_cart_id(
    client: TestClient, stdout_lines: io.StringIO
) -> None:
    response = client.post("/orders", params={"cartId": "cart-9"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown cart: cart-9"}
    (line,) = _lines(stdout_lines)
    assert line["severity"] == "WARNING"
    assert line["message"] == "unknown cart"
    assert line["cart_id"] == "cart-9"


def test_client_mistakes_keep_their_status_and_log_nothing(client: TestClient, stdout_lines: io.StringIO) -> None:
    assert client.get("/nope").status_code == 404
    assert client.post("/orders").status_code == 422
    assert client.post("/orders", params={"cartId": ""}).status_code == 422
    assert client.get("/orders").status_code == 405

    assert _lines(stdout_lines) == []


def test_unhandled_exception_is_500_with_one_trace_correlated_error_line(
    client: TestClient, stdout_lines: io.StringIO
) -> None:
    response = client.get("/boom", headers={"traceparent": "00-7d1f0e5a9c3b4a6e8f2d1c0b9a8e7f82-1111111111111111-01"})

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    (line,) = _lines(stdout_lines)
    assert line["severity"] == "ERROR"
    assert line["logger"] == "python_logging.main"
    assert line["message"].startswith("request failed\nTraceback (most recent call last):")
    assert "TimeoutError: read timed out" in line["message"]
    assert "The above exception was the direct cause of the following exception:" in line["message"]
    assert line["message"].rstrip().endswith("RuntimeError: payment gateway timed out")
    assert line[TRACE] == f"projects/{PROJECT_ID}/traces/7d1f0e5a9c3b4a6e8f2d1c0b9a8e7f82"


def test_health_probe_is_not_logged_by_the_access_logger(client: TestClient, stdout_lines: io.StringIO) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:1", "GET", "/healthz", "1.1", 200)

    assert _lines(stdout_lines) == []


def test_every_line_is_valid_json_even_with_a_broken_call_site(stdout_lines: io.StringIO) -> None:
    template = "ratio %s %s"  # one argument short, the way it happens in real code
    logging.getLogger("python_logging.main").info(template, float("nan"))

    (line,) = _lines(stdout_lines)
    assert line["log_format_error"].startswith("TypeError")
    assert json.loads(stdout_lines.getvalue())  # the buffer holds exactly one document
