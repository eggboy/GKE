"""Checkout domain rules: cart lookups, exact money, and unknown carts rejected consistently."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from checkout_api.main import CARTS, FLAT_SHIPPING_COST


@pytest.mark.parametrize(("cart_id", "items", "subtotal"), [("cart-1001", 2, "59.98"), ("cart-1002", 5, "149.50")])
def test_get_cart_returns_exact_amounts(client: TestClient, cart_id: str, items: int, subtotal: str) -> None:
    response = client.get(f"/carts/{cart_id}")

    assert response.status_code == 200
    assert response.json() == {"cart_id": cart_id, "items": items, "subtotal": subtotal}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/carts/cart-9999", None),
        ("POST", "/orders", {"cart_id": "cart-9999", "zip_code": "94043"}),
    ],
)
def test_unknown_cart_is_404_on_every_endpoint(client: TestClient, method: str, path: str, body: dict | None) -> None:
    response = client.request(method, path, json=body)

    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown cart: cart-9999"}


def test_shipping_quote_uses_the_flat_rate(client: TestClient) -> None:
    response = client.get("/shipping/quote", params={"zip_code": "94043"})

    assert response.status_code == 200
    assert response.json() == {"zip_code": "94043", "carrier": "demo-ground", "cost": str(FLAT_SHIPPING_COST)}


@pytest.mark.parametrize("cart_id", sorted(CARTS))
def test_order_total_is_subtotal_plus_flat_shipping_without_float_drift(client: TestClient, cart_id: str) -> None:
    response = client.post("/orders", json={"cart_id": cart_id, "zip_code": "10001"})

    assert response.status_code == 200
    body = response.json()
    assert body["order_id"].startswith("ord-")
    assert body["cart_id"] == cart_id
    assert body["zip_code"] == "10001"
    assert Decimal(body["total"]) == CARTS[cart_id][1] + FLAT_SHIPPING_COST
    assert body["total"] == str(CARTS[cart_id][1] + FLAT_SHIPPING_COST)


@pytest.mark.parametrize(("delay_ms", "status"), [(0, 200), (5000, 200), (-1, 422), (5001, 422)])
def test_delay_ms_bounds(client: TestClient, monkeypatch: pytest.MonkeyPatch, delay_ms: int, status: int) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("checkout_api.main.asyncio.sleep", no_sleep)
    response = client.post("/orders", json={"cart_id": "cart-1001", "zip_code": "94043", "delay_ms": delay_ms})
    assert response.status_code == status
