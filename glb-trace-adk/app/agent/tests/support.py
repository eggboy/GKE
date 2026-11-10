"""Helpers shared by the agent test modules (kept out of conftest so they can be imported explicitly)."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
from google.adk.events import Event
from google.genai import types

import main

USER_ID = "demo-shopper"


def trace_id_of(response: httpx.Response) -> str:
    """Trace ID from the W3C ``traceresponse`` header (``00-<trace-id>-<span-id>-<flags>``)."""
    return response.headers["traceresponse"].split("-")[1]


class FakeRunner:
    """Stands in for the ADK Runner: no Gemini call, same session service contract."""

    def __init__(self, session_service: Any, *, fail: bool = False) -> None:
        self.session_service = session_service
        self.fail = fail
        self.seen: list[tuple[str, str, str]] = []

    async def run_async(self, *, user_id: str, session_id: str, new_message: types.Content) -> AsyncIterator[Event]:
        self.seen.append((user_id, session_id, new_message.parts[0].text or ""))
        if self.fail:
            raise RuntimeError("model unavailable")
        yield Event(
            author="storefront_agent",
            content=types.Content(role="model", parts=[types.Part(text="Order ord-1 placed, total 67.97.")]),
        )


def remaining_sessions() -> list:
    service = main._direct_runner.session_service
    return asyncio.run(service.list_sessions(app_name=main.APP_NAME, user_id=USER_ID)).sessions
