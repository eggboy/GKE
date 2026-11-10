"""Shared fixtures.

Set ``GOOGLE_CLOUD_PROJECT`` before importing ``python_logging.main``, which configures logging at import time. The
session instruments the FastAPI app once: under pytest there is no ``opentelemetry-instrument`` wrapper, and Starlette
refuses new middleware after the first request.
"""

from tests.support import ensure_project_env

ensure_project_env()

import io
import logging
import sys
import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from python_logging.main import app
from python_logging.structured_logging import configure_logging, install_trace_record_factory

_EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="session", autouse=True)
def _instrumented_app() -> Iterator[None]:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls="/healthz$")
    yield
    FastAPIInstrumentor.uninstrument_app(app)


@pytest.fixture
def quiet_capture_handler() -> Iterator[None]:
    """Keep pytest's own capture handler from failing a test on a record the formatter under test handles.

    pytest attaches its handler to all loggers, non-propagating ones included, and re-raises when the standard
    formatter cannot render a record (a ``%s`` short of an argument, for example). That is the exact case the
    formatter's fallback line exists for, so the fixture switches the re-raise off while a test runs.
    """
    previous = logging.raiseExceptions
    logging.raiseExceptions = False
    yield
    logging.raiseExceptions = previous


@pytest.fixture
def restore_logging_state(quiet_capture_handler: None) -> Iterator[None]:  # noqa: ARG001 - ordering
    """configure_logging mutates process-wide logger state; undo it without disturbing pytest's own root handlers."""
    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx")
    root = logging.getLogger()
    saved_root = (list(root.handlers), root.level)
    saved = {name: _logger_state(logging.getLogger(name)) for name in names}
    saved_hooks = (logging.getLogRecordFactory(), sys.excepthook, threading.excepthook)
    yield
    logging.setLogRecordFactory(saved_hooks[0])
    sys.excepthook = saved_hooks[1]
    threading.excepthook = saved_hooks[2]
    root.handlers = saved_root[0]
    root.setLevel(saved_root[1])
    for name, (handlers, filters, propagate, level) in saved.items():
        named = logging.getLogger(name)
        named.handlers = handlers
        named.filters = filters
        named.propagate = propagate
        named.setLevel(level)
    logging.captureWarnings(False)


def _logger_state(named: logging.Logger) -> tuple[list[logging.Handler], list[logging.Filter], bool, int]:
    return list(named.handlers), list(named.filters), named.propagate, named.level


@pytest.fixture
def trace_stamps() -> Iterator[None]:
    """Stamp every record with the span of its logging call, the way configure_logging sets it up."""
    previous = logging.getLogRecordFactory()
    install_trace_record_factory()
    yield
    logging.setLogRecordFactory(previous)


@pytest.fixture
def no_trace_stamps() -> Iterator[None]:
    """Create plain records, as in a process that uses the formatter without configure_logging."""
    previous = logging.getLogRecordFactory()
    logging.setLogRecordFactory(logging.LogRecord)
    yield
    logging.setLogRecordFactory(previous)


@pytest.fixture
def stdout_lines(restore_logging_state: None) -> io.StringIO:  # noqa: ARG001 - ordering: restore after the test
    """Route the application's logging into a buffer, exactly as configure_logging does for stdout."""
    buffer = io.StringIO()
    configure_logging(stream=buffer)
    return buffer


@pytest.fixture
def finished_spans() -> InMemorySpanExporter:
    """The spans the instrumented app exports, starting empty for each test."""
    _EXPORTER.clear()
    return _EXPORTER


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)
