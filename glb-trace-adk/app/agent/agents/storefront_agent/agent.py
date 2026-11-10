"""Storefront ADK shopping agent with async REST tools calling checkout-api."""

import functools
import logging
import os
from typing import Any

import httpx
from google.adk.agents import Agent
from google.adk.tools import BaseTool, ToolContext
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)

CHECKOUT_API_URL = os.getenv("CHECKOUT_API_URL", "http://checkout-api.p1-demo.svc.cluster.local")
CHECKOUT_TIMEOUT_S = 15.0
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

INSTRUCTION = (
    "You are the storefront checkout assistant. Use the checkout tools for every "
    "cart, shipping, or order question. Always call place_order when the user asks "
    "to check out, then summarize the order id and total in one sentence."
)


def _new_checkout_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=CHECKOUT_API_URL, timeout=CHECKOUT_TIMEOUT_S)


@functools.cache
def checkout_client() -> httpx.AsyncClient:
    """One connection pool to the internal ALB per process, created on first use inside the running event loop."""
    return _new_checkout_client()


async def aclose_checkout_client() -> None:
    """Close the shared pool (called from the FastAPI lifespan); a later call to checkout_client() makes a new one."""
    if checkout_client.cache_info().currsize:
        await checkout_client().aclose()
        checkout_client.cache_clear()


def apply_demo_delay(tool: BaseTool, args: dict[str, Any], tool_context: ToolContext) -> dict[str, Any] | None:
    """Forward synthetic backend latency from session state into place_order."""
    if tool.name == "place_order" and "delay_ms" in tool_context.state:
        args["delay_ms"] = int(tool_context.state["delay_ms"])
    return None


async def _call_checkout(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send an instrumented HTTP request to checkout-api via the internal ALB."""
    try:
        response = await checkout_client().request(method, path, params=params, json=json_body)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # The tool returns the failure to Gemini on purpose so the agent can explain it; the span event alone is
        # invisible in Logs Explorer, so also leave a trace-correlated log line.
        status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        logger.warning("checkout-api %s %s failed: status=%s error=%s", method, path, status_code, exc)
        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, str(exc)))
        return {
            "status": "error",
            "http_status": status_code,
            "detail": str(exc),
        }
    result: dict[str, Any] = response.json()
    return result


async def get_cart(cart_id: str) -> dict[str, Any]:
    """Return item count and subtotal for a shopping cart ID."""
    return await _call_checkout("GET", f"/carts/{cart_id}")


async def quote_shipping(zip_code: str) -> dict[str, Any]:
    """Quote flat-rate shipping for a destination US ZIP code."""
    return await _call_checkout("GET", "/shipping/quote", params={"zip_code": zip_code})


async def place_order(cart_id: str, zip_code: str, delay_ms: int = 0) -> dict[str, Any]:
    """Place a checkout order for cart_id shipped to zip_code."""
    return await _call_checkout(
        "POST",
        "/orders",
        json_body={
            "cart_id": cart_id,
            "zip_code": zip_code,
            "delay_ms": delay_ms,
        },
    )


root_agent = Agent(
    name="storefront_agent",
    model=GEMINI_MODEL,
    description="Storefront shopping agent backed by checkout-api behind GKE Regional Internal ALB.",
    instruction=INSTRUCTION,
    tools=[get_cart, quote_shipping, place_order],
    before_tool_callback=apply_demo_delay,
)
