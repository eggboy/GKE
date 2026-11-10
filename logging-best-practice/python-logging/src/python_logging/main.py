"""FastAPI service that shows the formatter at work: a line with fields, a 404, and an unhandled exception.

Importing this module configures logging before the first record, so uvicorn's own startup lines are JSON as well.
Set ``GOOGLE_CLOUD_PROJECT`` before the import.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic import BaseModel

from python_logging.structured_logging import configure_logging
from python_logging.tracing import SpanQueryRedactionMiddleware

configure_logging()
logger = logging.getLogger(__name__)

FLAT_TOTAL = Decimal("67.97")


class UnknownCartError(Exception):
    """Raised when a requested shopping cart ID does not exist."""

    def __init__(self, cart_id: str) -> None:
        """Keep the offending cart ID for the response and the log line."""
        super().__init__(f"Unknown cart: {cart_id}")
        self.cart_id = cart_id


class Health(BaseModel):
    """Liveness and readiness payload."""

    status: str


class Order(BaseModel):
    """Order confirmation payload."""

    order_id: str
    cart_id: str
    total: Decimal


async def log_unhandled_exceptions() -> AsyncIterator[None]:
    """Log an exception without a handler while the request span is still active, then answer 500.

    Left alone, the exception travels out through the OpenTelemetry middleware, which ends the span, and uvicorn logs
    it afterwards as ``Exception in ASGI application`` with no trace fields. Catching it here, inside the span, gives
    one ERROR line that Logs Explorer shows next to the trace. Exceptions with a registered handler (``HTTPException``,
    validation errors, :class:`UnknownCartError`) pass through untouched: they are answers to the client.
    """
    try:
        yield
    except Exception as exc:
        if _has_handler(exc):
            raise
        logger.exception("request failed")
        trace.get_current_span().record_exception(exc)
        raise HTTPException(status_code=500, detail="Internal Server Error") from exc


def _has_handler(exc: Exception) -> bool:
    return any(cls in app.exception_handlers for cls in type(exc).__mro__)


app = FastAPI(title="python-logging", dependencies=[Depends(log_unhandled_exceptions)])
# The access-log filter keeps query strings out of the log; this keeps their values out of the trace.
app.add_middleware(SpanQueryRedactionMiddleware)


@app.exception_handler(UnknownCartError)
async def handle_unknown_cart(_: Request, exc: UnknownCartError) -> JSONResponse:
    """A client mistake is a 404 and a WARNING line, not an error."""
    logger.warning("unknown cart", extra={"cart_id": exc.cart_id})
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.get("/healthz")
async def healthz() -> Health:
    """Unauthenticated liveness endpoint for probes; its access records are dropped."""
    return Health(status="ok")


@app.post("/orders")
async def place_order(cart_id: Annotated[str, Query(alias="cartId", min_length=1)]) -> Order:
    """``extra`` keys become top-level JSON fields."""
    if cart_id == "cart-9":
        raise UnknownCartError(cart_id)
    order = Order(order_id=f"ord-{uuid.uuid4().hex[:8]}", cart_id=cart_id, total=FLAT_TOTAL)
    logger.info(
        "placed order",
        extra={"order_id": order.order_id, "cart_id": order.cart_id, "total": float(order.total)},
    )
    return order


@app.get("/boom")
async def boom() -> None:
    """An exception with a cause that nothing handles: one ERROR line, traceback in ``message``, trace attached."""
    try:
        _read_payment_gateway()
    except TimeoutError as exc:
        raise RuntimeError("payment gateway timed out") from exc


def _read_payment_gateway() -> None:
    raise TimeoutError("read timed out")
