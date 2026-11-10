"""CVE-2026-114603 regression: a pathological ``tracestate`` header must not stall the server.

opentelemetry-api < 1.45.1 parsed ``tracestate`` in O(N²); google-adk's ``opentelemetry-api<=1.44`` cap is overridden
in pyproject.toml to install the fixed release. This test fails if that override is ever dropped while ADK still caps
the SDK below 1.45.1 (the two requests below would take 6-12 s instead of milliseconds).
"""

import time

import pytest
from fastapi.testclient import TestClient

import main
from tests.support import trace_id_of

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.mark.usefixtures("fake_runner")
def test_pathological_tracestate_does_not_stall_a_traced_endpoint() -> None:
    client = TestClient(main.app)
    started = time.monotonic()

    response = client.get("/order", headers={"traceparent": TRACEPARENT, "tracestate": " " * 100_000})
    response_tabs = client.get("/order", headers={"traceparent": TRACEPARENT, "tracestate": "\t" * 60_000})

    assert response.status_code == 200
    assert response_tabs.status_code == 200
    assert trace_id_of(response) == "4bf92f3577b34da6a3ce929d0e0e4736", "traceparent must survive"
    assert time.monotonic() - started < 3, "unmitigated, a 100 KB tracestate costs 6-12 s of CPU per request"
