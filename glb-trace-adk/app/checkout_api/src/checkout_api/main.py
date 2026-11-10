"""FastAPI REST checkout service behind the GKE Regional Internal ALB. """

import asyncio
import logging
import uuid
from decimal import Decimal
from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from checkout_api.structured_logging import configure_logging

load_dotenv()
configure_logging()
logger = logging.getLogger(__name__)

Money = Annotated[Decimal, Field(ge=0, decimal_places=2)]
FLAT_SHIPPING_COST = Decimal("7.99")
CARTS: dict[str, tuple[int, Decimal]] = {
    "cart-1001": (2, Decimal("59.98")),
    "cart-1002": (5, Decimal("149.50")),
}


class UnknownCartError(Exception):
    """Raised when a requested shopping cart ID does not exist."""

    def __init__(self, cart_id: str) -> None:
        """Keep the offending cart ID for the HTTP 404 body."""
        super().__init__(cart_id)
        self.cart_id = cart_id


class HealthResponse(BaseModel):
    """Liveness and readiness payload."""

    status: str


class CartResponse(BaseModel):
    """Shopping cart summary."""

    cart_id: str
    items: int
    subtotal: Money


class ShippingQuoteResponse(BaseModel):
    """Flat-rate shipping quote."""

    zip_code: str
    carrier: str
    cost: Money


class PlaceOrderRequest(BaseModel):
    """Payload to create an order."""

    cart_id: str
    zip_code: str
    delay_ms: int = Field(default=0, ge=0, le=5000)


class PlaceOrderResponse(BaseModel):
    """Order confirmation payload."""

    order_id: str
    cart_id: str
    zip_code: str
    total: Money


app = FastAPI(title="Checkout REST API (GKE LB tracing demo)")


@app.exception_handler(UnknownCartError)
async def handle_unknown_cart(_: Request, exc: UnknownCartError) -> JSONResponse:
    """Return HTTP 404 JSON when a cart ID is missing."""
    return JSONResponse(status_code=404, content={"detail": f"Unknown cart: {exc.cart_id}"})


@app.get("/healthz")
async def healthz() -> HealthResponse:
    """Unauthenticated liveness endpoint for GKE Gateway health checks."""
    return HealthResponse(status="ok")


@app.get("/carts/{cart_id}", responses={404: {"description": "Cart not found"}})
async def get_cart(cart_id: Annotated[str, Path(min_length=1)]) -> CartResponse:
    """Return item count and subtotal for a cart ID."""
    cart = CARTS.get(cart_id)
    if cart is None:
        raise UnknownCartError(cart_id)
    items, subtotal = cart
    return CartResponse(cart_id=cart_id, items=items, subtotal=subtotal)


@app.get("/shipping/quote")
async def quote_shipping(zip_code: Annotated[str, Query(min_length=3)]) -> ShippingQuoteResponse:
    """Return flat-rate shipping quote for a ZIP code."""
    return ShippingQuoteResponse(zip_code=zip_code, carrier="demo-ground", cost=FLAT_SHIPPING_COST)


@app.post("/orders", responses={404: {"description": "Cart not found"}})
async def place_order(req: PlaceOrderRequest) -> PlaceOrderResponse:
    """Create an order with optional artificial latency for trace attribution."""
    cart = CARTS.get(req.cart_id)
    if cart is None:
        raise UnknownCartError(req.cart_id)
    if req.delay_ms:
        await asyncio.sleep(req.delay_ms / 1000)
    _, subtotal = cart
    order_id = f"ord-{uuid.uuid4().hex[:8]}"
    total = subtotal + FLAT_SHIPPING_COST
    logger.info("placed order=%s cart=%s total=%s", order_id, req.cart_id, total)
    return PlaceOrderResponse(
        order_id=order_id,
        cart_id=req.cart_id,
        zip_code=req.zip_code,
        total=total,
    )
