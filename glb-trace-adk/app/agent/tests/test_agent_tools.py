"""Agent tools: the shared httpx pool, error handling of checkout-api calls, and the demo-delay callback."""

import asyncio
import logging
from collections.abc import Iterator
from types import SimpleNamespace

import httpx
import pytest

from storefront_agent import agent as agent_module

CART_BODY = {"cart_id": "cart-1001", "items": 2, "subtotal": "59.98"}


@pytest.fixture
def checkout_requests(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[httpx.Request]]:
    """Route the shared client at an in-process fake checkout-api and collect what it receives."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/orders":
            return httpx.Response(503, json={"detail": "no healthy upstream"})
        return httpx.Response(200, json=CART_BODY)

    def fake_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="http://checkout.test", transport=httpx.MockTransport(handler))

    monkeypatch.setattr(agent_module, "_new_checkout_client", fake_client)
    agent_module.checkout_client.cache_clear()
    yield requests
    asyncio.run(agent_module.aclose_checkout_client())


def test_get_cart_returns_the_body_and_reuses_one_client(checkout_requests: list[httpx.Request]) -> None:
    async def two_calls() -> tuple[dict, dict]:
        return await agent_module.get_cart("cart-1001"), await agent_module.get_cart("cart-1001")

    first, second = asyncio.run(two_calls())

    assert first == CART_BODY
    assert second == CART_BODY
    assert [r.url.path for r in checkout_requests] == ["/carts/cart-1001", "/carts/cart-1001"]
    assert agent_module.checkout_client() is agent_module.checkout_client()


def test_place_order_failure_is_returned_to_the_model_and_logged(
    checkout_requests: list[httpx.Request], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=agent_module.logger.name):
        result = asyncio.run(agent_module.place_order("cart-1001", "94043", delay_ms=1500))

    assert result["status"] == "error"
    assert result["http_status"] == 503
    assert "503" in result["detail"]
    assert checkout_requests[-1].read() == b'{"cart_id":"cart-1001","zip_code":"94043","delay_ms":1500}'
    assert "checkout-api POST /orders failed: status=503" in caplog.text


@pytest.mark.usefixtures("checkout_requests")
def test_aclose_closes_the_pool_and_the_next_call_gets_a_fresh_one() -> None:
    first = agent_module.checkout_client()
    asyncio.run(agent_module.aclose_checkout_client())

    assert first.is_closed
    assert agent_module.checkout_client() is not first
    asyncio.run(agent_module.aclose_checkout_client())  # idempotent
    asyncio.run(agent_module.aclose_checkout_client())
    assert agent_module.checkout_client.cache_info().currsize == 0


@pytest.mark.parametrize(
    ("tool_name", "state", "expected_args"),
    [
        ("place_order", {"delay_ms": 1500}, {"cart_id": "cart-1001", "delay_ms": 1500}),
        ("place_order", {"delay_ms": "250"}, {"cart_id": "cart-1001", "delay_ms": 250}),
        ("place_order", {}, {"cart_id": "cart-1001"}),
        ("get_cart", {"delay_ms": 1500}, {"cart_id": "cart-1001"}),
    ],
)
def test_apply_demo_delay_only_touches_place_order(tool_name: str, state: dict, expected_args: dict) -> None:
    args = {"cart_id": "cart-1001"}
    tool = SimpleNamespace(name=tool_name)
    tool_context = SimpleNamespace(state=state)

    assert agent_module.apply_demo_delay(tool, args, tool_context) is None
    assert args == expected_args
