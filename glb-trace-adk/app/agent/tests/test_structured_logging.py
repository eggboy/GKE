"""Cloud Logging JSON formatter and root logging configuration."""

import io
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, Sampler

from structured_logging import CloudLoggingJsonFormatter, configure_logging

PROJECT_ID = "unit-test-project"
EmitFn = Callable[..., str]


@pytest.fixture
def emit() -> Iterator[EmitFn]:
    """Log through an isolated logger with the formatter under test and return the raw line it wrote."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(CloudLoggingJsonFormatter(PROJECT_ID))
    logger = logging.getLogger("tests.structured_logging")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    def _emit(level: int, msg: str, *args: object, **kwargs: Any) -> str:
        stream.seek(0)
        stream.truncate()
        logger.log(level, msg, *args, **kwargs)
        return stream.getvalue()

    yield _emit
    logger.handlers = []


def _payload(line: str) -> dict[str, Any]:
    assert line.endswith("\n")
    assert "\n" not in line[:-1], "Cloud Logging needs one JSON document per line"
    return json.loads(line)


def test_record_outside_a_span_has_reserved_fields_and_no_trace_keys(emit: EmitFn) -> None:
    payload = _payload(emit(logging.WARNING, "cart %s missing", "cart-9"))

    assert payload["severity"] == "WARNING"
    assert payload["message"] == "cart cart-9 missing"
    assert payload["logger"] == "tests.structured_logging"
    assert payload["timestamp"].endswith("+00:00")
    assert payload["logging.googleapis.com/sourceLocation"]["function"] == "_emit"
    assert not any(key.startswith("logging.googleapis.com/trace") for key in payload)
    assert "logging.googleapis.com/spanId" not in payload


@pytest.mark.parametrize(("sampler", "sampled"), [(ALWAYS_ON, True), (ALWAYS_OFF, False)])
def test_record_inside_a_span_carries_full_trace_resource_name(emit: EmitFn, sampler: Sampler, sampled: bool) -> None:
    tracer = TracerProvider(sampler=sampler).get_tracer("tests")
    with tracer.start_as_current_span("checkout") as span:
        context = span.get_span_context()
        payload = _payload(emit(logging.INFO, "placed order"))

    assert payload["logging.googleapis.com/trace"] == f"projects/{PROJECT_ID}/traces/{context.trace_id:032x}"
    assert payload["logging.googleapis.com/spanId"] == f"{context.span_id:016x}"
    assert payload["logging.googleapis.com/trace_sampled"] is sampled
    assert trace.get_current_span() is trace.INVALID_SPAN


def test_exception_traceback_and_stack_info_are_kept(emit: EmitFn) -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        payload = _payload(emit(logging.ERROR, "order failed", exc_info=True))
    assert payload["severity"] == "ERROR"
    assert payload["message"].startswith("order failed\nTraceback (most recent call last):")
    assert payload["message"].rstrip().endswith("ValueError: boom")

    payload = _payload(emit(logging.INFO, "where am I", stack_info=True))
    assert "Stack (most recent call last):" in payload["message"]


def test_extra_fields_become_top_level_keys_but_cannot_shadow_reserved_ones(emit: EmitFn) -> None:
    extra = {"order_id": "ord-1", "total": 67.97, "severity": "DEBUG", "_private": 1, "color_message": "\x1b[1mplaced"}
    payload = _payload(emit(logging.INFO, "placed", extra=extra))

    assert payload["order_id"] == "ord-1"
    assert payload["total"] == 67.97
    assert payload["severity"] == "INFO"
    assert "_private" not in payload
    assert "color_message" not in payload, "uvicorn's ANSI copy of the message is noise in Cloud Logging"


def test_non_json_extra_values_fall_back_to_str(emit: EmitFn) -> None:
    payload = _payload(emit(logging.INFO, "x", extra={"when": object()}))
    assert payload["when"].startswith("<object object")


@pytest.fixture
def restore_logging_state() -> Iterator[None]:
    """configure_logging mutates process-wide logger state; undo it without disturbing pytest's own root handlers."""
    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx")
    saved = {name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate) for name in names}
    levels = {name: logging.getLogger(name).level for name in ("", *names)}
    yield
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not isinstance(h.formatter, CloudLoggingJsonFormatter)]
    for name, (handlers, propagate) in saved.items():
        logging.getLogger(name).handlers = handlers
        logging.getLogger(name).propagate = propagate
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.usefixtures("restore_logging_state")
def test_configure_logging_routes_uvicorn_through_one_json_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT_ID)
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.addHandler(logging.NullHandler())
    uvicorn_access.propagate = False

    configure_logging()

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, CloudLoggingJsonFormatter)
    assert root.level == logging.INFO
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True
    assert logging.getLogger("httpx").level == logging.WARNING


@pytest.mark.usefixtures("restore_logging_state")
def test_configure_logging_fails_fast_without_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(KeyError, match="GOOGLE_CLOUD_PROJECT"):
        configure_logging()
