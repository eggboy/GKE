"""Shared fixtures.

``GOOGLE_CLOUD_PROJECT`` must exist before ``checkout_api.main`` is imported because logging is configured at import
time. The FastAPI app is instrumented explicitly and once per session: under pytest there is no
``opentelemetry-instrument`` wrapper, and Starlette refuses new middleware after the first request.
"""

import os

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from checkout_api.main import app

_EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="session", autouse=True)
def _instrumented_app() -> Iterator[None]:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls="healthz")
    yield
    FastAPIInstrumentor.uninstrument_app(app)


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    """Finished spans of the current test only."""
    _EXPORTER.clear()
    return _EXPORTER


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
