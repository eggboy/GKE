"""ADK FastAPI entrypoint using GKE Managed OpenTelemetry plus LB trace propagation. """

from dotenv import load_dotenv

load_dotenv()

import logging
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Query
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.propagators import TraceResponsePropagator, set_global_response_propagator
from pydantic import BaseModel, Field

from structured_logging import configure_logging

configure_logging()
logger = logging.getLogger(__name__)

APP_NAME = "storefront_agent"
AGENTS_DIR = Path(__file__).resolve().parent / "agents"

# Import the agent the same way ADK's loader does (agents_dir on sys.path, top-level package name) so both resolve
# to one module and one root_agent instead of two copies under different module names.
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))
from storefront_agent.agent import aclose_checkout_client, root_agent


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Close the shared httpx pool to checkout-api on shutdown (ADK wraps this in its own lifespan)."""
    yield
    await aclose_checkout_client()

app = get_fast_api_app(agents_dir=str(AGENTS_DIR), web=True, lifespan=lifespan)

# Required for Load Balancer trace continuity on GKE:
# 1. Extract inbound W3C traceparent from the Global External ALB.
# 2. Inject outbound W3C traceparent into httpx calls to the Regional Internal ALB.
# 3. Answer with a W3C ``traceresponse`` header (Trace Context Level 2) built from the server span, so clients and the
#    demo scripts read the trace ID from the transport instead of from the response body. The ASGI instrumentation
#    injects it on every ``http.response.start`` once a global response propagator is registered.
# Health checks, the app list and the dev UI's static assets are not worth a span each.
set_global_response_propagator(TraceResponsePropagator())
HTTPXClientInstrumentor().instrument()
FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,list-apps,dev-ui")

_direct_runner = Runner(
    app_name=APP_NAME,
    agent=root_agent,
    session_service=InMemorySessionService(),
)


class HealthResponse(BaseModel):
    """Liveness and readiness payload."""

    status: str


class OrderQuery(BaseModel):
    """Query parameters for CLI trace verification."""

    prompt: str = Field(
        default="Check out cart cart-1001 and ship it to ZIP 94043.",
        max_length=2000,
    )
    delay_ms: int = Field(default=0, ge=0, le=5000)


class ToolCallSummary(BaseModel):
    """Summary of one ADK function tool invocation."""

    name: str
    args: dict[str, Any]


class OrderResponse(BaseModel):
    """Outcome of one agent turn: business payload only. """

    agent_reply: str
    tool_calls: list[ToolCallSummary]


@app.get("/healthz")
async def healthz() -> HealthResponse:
    """Unauthenticated HTTP 200 for GKE Gateway health checks."""
    return HealthResponse(status="ok")


@app.get("/order")
async def order(query: Annotated[OrderQuery, Query()]) -> OrderResponse:
    """Single-request CLI endpoint executing root_agent under the active ALB trace."""
    user_id = "demo-shopper"
    session_service = _direct_runner.session_service
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=str(uuid.uuid4()),
        state={"delay_ms": query.delay_ms},
    )
    message = types.Content(role="user", parts=[types.Part(text=query.prompt)])

    reply_parts: list[str] = []
    tool_calls: list[ToolCallSummary] = []
    try:
        async for event in _direct_runner.run_async(user_id=user_id, session_id=session.id, new_message=message):
            for call in event.get_function_calls():
                tool_calls.append(ToolCallSummary(name=call.name, args=dict(call.args or {})))
            if event.is_final_response() and event.content and event.content.parts:
                reply_parts.extend(part.text for part in event.content.parts if part.text)
    finally:
        # Every /order call is a throwaway conversation; without this the in-memory store grows for the pod's lifetime.
        await session_service.delete_session(app_name=APP_NAME, user_id=user_id, session_id=session.id)

    logger.info("agent turn complete tools=%d", len(tool_calls))
    return OrderResponse(agent_reply="".join(reply_parts), tool_calls=tool_calls)
