"""The ADK FastAPI app: single agent module, health endpoint, and /order session hygiene."""

import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

import main
from tests.support import USER_ID, FakeRunner, trace_id_of


def test_agent_module_is_imported_once_and_shared_with_adk() -> None:
    assert "agents.storefront_agent.agent" not in sys.modules
    assert sys.modules["storefront_agent.agent"].root_agent is main.root_agent
    assert main._direct_runner.agent is main.root_agent


def test_healthz() -> None:
    assert TestClient(main.app).get("/healthz").json() == {"status": "ok"}


def test_order_runs_the_agent_once_and_keeps_trace_metadata_out_of_the_body(fake_runner: FakeRunner) -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    response = TestClient(main.app).get(
        "/order", params={"prompt": "Check out cart cart-1002.", "delay_ms": 250}, headers={"traceparent": traceparent}
    )

    assert response.status_code == 200
    assert response.json() == {
        "agent_reply": "Order ord-1 placed, total 67.97.",
        "tool_calls": [],
    }
    assert trace_id_of(response) == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert response.headers["access-control-expose-headers"] == "traceresponse"
    assert len(fake_runner.seen) == 1
    assert fake_runner.seen[0][0] == USER_ID
    assert fake_runner.seen[0][2] == "Check out cart cart-1002."


@pytest.mark.parametrize("fake_runner", [True], indirect=True)
def test_order_deletes_the_session_even_when_the_agent_fails(fake_runner: FakeRunner) -> None:
    with pytest.raises(RuntimeError, match="model unavailable"):
        TestClient(main.app).get("/order")
    assert len(fake_runner.seen) == 1


@pytest.mark.parametrize("params", [{"delay_ms": 5001}, {"delay_ms": -1}, {"prompt": "x" * 2001}])
def test_order_rejects_out_of_range_query_parameters(params: dict[str, Any]) -> None:
    assert TestClient(main.app).get("/order", params=params).status_code == 422
