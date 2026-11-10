"""Cloud Logging JSON formatter and root logging configuration."""

import io
import json
import logging
import logging.handlers
import queue
import warnings
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, Sampler

from python_logging.structured_logging import (
    FORMAT_ERROR,
    LABELS,
    SOURCE_LOCATION,
    SPAN_ID,
    TIMESTAMP_NANOS,
    TIMESTAMP_SECONDS,
    TRACE,
    TRACE_SAMPLED,
    AccessLogPathFilter,
    CloudLoggingJsonFormatter,
    configure_logging,
)
from tests.support import PROJECT_ID, parse_line

EmitFn = Callable[..., str]
AGENT_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
AGENT_SPAN_ID = "00f067aa0ba902b7"


class Explosive:
    def __str__(self) -> str:
        raise RuntimeError("str failed")


@pytest.fixture
def emit(quiet_capture_handler: None) -> Iterator[EmitFn]:  # noqa: ARG001 - ordering
    """Log through an isolated logger with the formatter under test and return the raw line it wrote."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(CloudLoggingJsonFormatter(PROJECT_ID))
    logger = logging.getLogger("tests.structured_logging")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    def _emit(level: int, msg: object, *args: object, formatter: logging.Formatter | None = None, **kwargs: Any) -> str:
        if formatter is not None:
            handler.setFormatter(formatter)
        stream.seek(0)
        stream.truncate()
        logger.log(level, msg, *args, **kwargs)
        return stream.getvalue()

    yield _emit
    logger.handlers = []


def _payload(line: str) -> dict[str, Any]:
    return parse_line(line)


def test_record_outside_a_span_has_reserved_fields_and_no_trace_keys(emit: EmitFn) -> None:
    payload = _payload(emit(logging.WARNING, "cart %s missing", "cart-9"))

    assert payload["severity"] == "WARNING"
    assert payload["message"] == "cart cart-9 missing"
    assert payload["logger"] == "tests.structured_logging"
    assert payload["timestamp"].endswith("Z")
    assert payload[SOURCE_LOCATION]["function"] == "_emit"
    assert payload[SOURCE_LOCATION]["file"].endswith("test_structured_logging.py")
    assert not any(key.startswith("logging.googleapis.com/trace") for key in payload)
    assert SPAN_ID not in payload
    assert FORMAT_ERROR not in payload


def test_timestamp_pair_and_timestamp_string_are_the_instant_of_the_record(emit: EmitFn) -> None:
    payload = _payload(emit(logging.INFO, "placed"))

    seconds, nanos = payload[TIMESTAMP_SECONDS], payload[TIMESTAMP_NANOS]
    assert isinstance(seconds, int)
    assert isinstance(nanos, int)
    assert 0 <= nanos < 1_000_000_000
    from_pair = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanos // 1000)
    assert datetime.fromisoformat(payload["timestamp"]) == from_pair
    assert abs(from_pair.timestamp() - datetime.now(tz=UTC).timestamp()) < 5


@pytest.mark.parametrize(("sampler", "sampled"), [(ALWAYS_ON, True), (ALWAYS_OFF, False)])
def test_record_inside_a_span_carries_full_trace_resource_name(emit: EmitFn, sampler: Sampler, sampled: bool) -> None:
    tracer = TracerProvider(sampler=sampler).get_tracer("tests")
    with tracer.start_as_current_span("checkout") as span:
        context = span.get_span_context()
        payload = _payload(emit(logging.INFO, "placed order"))

    assert payload[TRACE] == f"projects/{PROJECT_ID}/traces/{context.trace_id:032x}"
    assert payload[SPAN_ID] == f"{context.span_id:016x}"
    assert payload[TRACE_SAMPLED] is sampled
    assert trace.get_current_span() is trace.INVALID_SPAN


def test_record_attributes_from_the_logging_instrumentation_win_over_the_current_span(emit: EmitFn) -> None:
    tracer = TracerProvider(sampler=ALWAYS_ON).get_tracer("tests")
    with tracer.start_as_current_span("checkout"):
        payload = _payload(
            emit(
                logging.INFO,
                "stamped at creation",
                extra={"otelTraceID": AGENT_TRACE_ID, "otelSpanID": AGENT_SPAN_ID, "otelTraceSampled": False},
            )
        )

    assert payload[TRACE] == f"projects/{PROJECT_ID}/traces/{AGENT_TRACE_ID}"
    assert payload[SPAN_ID] == AGENT_SPAN_ID
    assert payload[TRACE_SAMPLED] is False
    assert not any(key.startswith("otel") for key in payload)


def test_zero_record_attributes_mean_no_span(emit: EmitFn) -> None:
    payload = _payload(
        emit(logging.INFO, "no span", extra={"otelTraceID": "0", "otelSpanID": "0", "otelTraceSampled": False})
    )

    assert TRACE not in payload
    assert not any(key.startswith("otel") for key in payload)


def test_queue_handler_keeps_the_trace_through_the_record_attributes() -> None:
    stream = io.StringIO()
    target = logging.StreamHandler(stream)
    target.setFormatter(CloudLoggingJsonFormatter(PROJECT_ID))
    records: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    listener = logging.handlers.QueueListener(records, target)
    logger = logging.getLogger("tests.queued")
    logger.handlers = [logging.handlers.QueueHandler(records)]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    listener.start()
    try:
        tracer = TracerProvider(sampler=ALWAYS_ON).get_tracer("tests")
        with tracer.start_as_current_span("checkout"):
            try:
                raise ValueError("boom")
            except ValueError:
                logger.exception("queued", extra={"otelTraceID": AGENT_TRACE_ID, "otelSpanID": AGENT_SPAN_ID})
    finally:
        listener.stop()
        logger.handlers = []
    payload = _payload(stream.getvalue())

    assert payload[TRACE] == f"projects/{PROJECT_ID}/traces/{AGENT_TRACE_ID}"
    assert payload["message"].startswith("queued\nTraceback (most recent call last):")
    assert payload["message"].rstrip().endswith("ValueError: boom")


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


def test_chained_exception_keeps_the_cause(emit: EmitFn) -> None:
    try:
        try:
            raise KeyError("inner")
        except KeyError as exc:
            raise RuntimeError("outer") from exc
    except RuntimeError:
        payload = _payload(emit(logging.ERROR, "chained", exc_info=True))

    assert "KeyError: 'inner'" in payload["message"]
    assert "The above exception was the direct cause of the following exception:" in payload["message"]
    assert payload["message"].rstrip().endswith("RuntimeError: outer")


def test_long_traceback_is_cut_in_the_middle(emit: EmitFn) -> None:
    formatter = CloudLoggingJsonFormatter(PROJECT_ID, traceback_max_length=400)
    try:
        raise ValueError("body=" + "y" * 5_000)
    except ValueError:
        payload = _payload(emit(logging.ERROR, "request failed", exc_info=True, formatter=formatter))
    message = payload["message"]

    assert message.startswith("request failed\nTraceback (most recent call last):")
    assert message.rstrip().endswith("y")
    assert "characters omitted" in message
    assert len(message) < 600


def test_long_message_is_cut_at_the_configured_length(emit: EmitFn) -> None:
    formatter = CloudLoggingJsonFormatter(PROJECT_ID, message_max_length=10)
    try:
        raise ValueError("boom")
    except ValueError:
        payload = _payload(emit(logging.ERROR, "0123456789abcdef", exc_info=True, formatter=formatter))

    assert payload["message"].startswith("0123456789... [6 more characters]\nTraceback (most recent call last):")


def test_default_message_cap_keeps_a_huge_message_under_the_cloud_logging_entry_limit(emit: EmitFn) -> None:
    line = emit(logging.INFO, "payload=%s", "x" * 300_000)

    assert len(line.encode()) < 256 * 1024
    assert _payload(line)["message"].endswith(" more characters]")


def test_extra_fields_become_top_level_keys_but_cannot_shadow_reserved_ones(emit: EmitFn) -> None:
    extra = {
        "order_id": "ord-1",
        "total": 67.97,
        "severity": "DEBUG",
        "logger": "forged",
        "stream": "stderr",
        "time": "1999-01-01T00:00:00Z",
        TIMESTAMP_SECONDS: 1,
        TIMESTAMP_NANOS: 2,
        TRACE: "projects/forged/traces/1",
        "_private": 1,
        "color_message": "\x1b[1mplaced",
    }
    payload = _payload(emit(logging.INFO, "placed", extra=extra))

    assert payload["order_id"] == "ord-1"
    assert payload["total"] == 67.97
    assert payload["severity"] == "INFO"
    assert payload["logger"] == "tests.structured_logging"
    assert payload[TIMESTAMP_SECONDS] > 1
    assert TRACE not in payload
    for key in ("stream", "time", "_private", "color_message"):
        assert key not in payload


def test_labels_mapping_becomes_the_labels_member_with_string_values(emit: EmitFn) -> None:
    payload = _payload(emit(logging.INFO, "placed", extra={LABELS: {"team": "checkout", "attempt": 2}}))

    assert payload[LABELS] == {"team": "checkout", "attempt": "2"}


def test_labels_that_are_not_a_mapping_are_dropped(emit: EmitFn) -> None:
    payload = _payload(emit(logging.INFO, "placed", extra={LABELS: "not-a-mapping"}))

    assert LABELS not in payload


def test_non_json_extra_values_fall_back_to_str(emit: EmitFn) -> None:
    payload = _payload(emit(logging.INFO, "x", extra={"when": object()}))
    assert payload["when"].startswith("<object object")


def test_non_finite_numbers_are_written_as_strings(emit: EmitFn) -> None:
    extra = {
        "ratio": float("nan"),
        "limit": float("inf"),
        "stats": {"p99": float("-inf"), 7: "int key"},
        "samples": (1.5, float("nan")),
    }
    payload = _payload(emit(logging.INFO, "x", extra=extra))

    assert payload["ratio"] == "nan"
    assert payload["limit"] == "inf"
    assert payload["stats"] == {"p99": "-inf", "7": "int key"}
    assert payload["samples"] == [1.5, "nan"]


def test_non_ascii_is_written_as_utf8(emit: EmitFn) -> None:
    line = emit(logging.INFO, "주문 완료 %s", "ord-1", extra={"city": "서울"})

    assert "서울" in line
    assert _payload(line)["message"] == "주문 완료 ord-1"


@pytest.mark.parametrize(
    ("levelno", "severity"), [(15, "DEBUG"), (25, "INFO"), (35, "WARNING"), (45, "ERROR"), (60, "CRITICAL")]
)
def test_custom_levels_map_to_cloud_logging_severities(emit: EmitFn, levelno: int, severity: str) -> None:
    assert _payload(emit(levelno, "custom level"))["severity"] == severity


def test_bad_format_arguments_fall_back_to_a_reduced_line(emit: EmitFn, capsys: pytest.CaptureFixture[str]) -> None:
    payload = _payload(emit(logging.INFO, "value %s %s", "only-one", extra={"order_id": "ord-1"}))

    assert payload["message"] == "value %s %s"
    assert payload["log_args"] == "('only-one',)"
    assert payload[FORMAT_ERROR].startswith("TypeError: ")
    assert payload["severity"] == "INFO"
    assert "order_id" not in payload
    assert capsys.readouterr().err == "", "nothing may reach stderr"


def test_value_whose_str_raises_falls_back_to_a_reduced_line(emit: EmitFn, capsys: pytest.CaptureFixture[str]) -> None:
    payload = _payload(emit(logging.WARNING, "still logged", extra={"bad": Explosive()}))

    assert payload["message"] == "still logged"
    assert payload["severity"] == "WARNING"
    assert "RuntimeError: str failed" in payload[FORMAT_ERROR]
    assert capsys.readouterr().err == ""


def test_source_location_can_be_switched_off(emit: EmitFn) -> None:
    payload = _payload(emit(logging.INFO, "x", formatter=CloudLoggingJsonFormatter(PROJECT_ID, source_location=False)))

    assert SOURCE_LOCATION not in payload


def test_access_log_filter_drops_the_quiet_paths_only() -> None:
    quiet = AccessLogPathFilter(["/healthz"])
    make = logging.getLogger("uvicorn.access").makeRecord

    def access_record(path: str) -> logging.LogRecord:
        args = ("c", "GET", path, "1.1", 200)
        return make("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', args, None)

    assert quiet.filter(access_record("/healthz")) is False
    assert quiet.filter(access_record("/healthz?verbose=1")) is False
    assert quiet.filter(access_record("/orders")) is True
    assert quiet.filter(make("uvicorn.access", logging.INFO, "", 0, "plain %s", ("x",), None)) is True


@pytest.mark.usefixtures("restore_logging_state")
def test_configure_logging_routes_uvicorn_and_warnings_through_one_json_handler() -> None:
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.addHandler(logging.NullHandler())
    uvicorn_access.propagate = False
    buffer = io.StringIO()

    configure_logging(stream=buffer)
    warnings.warn("deprecated knob", DeprecationWarning, stacklevel=1)
    uvicorn_access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:1", "GET", "/healthz", "1.1", 200)
    uvicorn_access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:1", "POST", "/orders?cartId=1", "1.1", 200)

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, CloudLoggingJsonFormatter)
    assert root.level == logging.INFO
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True
    assert logging.getLogger("httpx").level == logging.WARNING
    lines = [json.loads(line) for line in buffer.getvalue().splitlines()]
    assert [line["logger"] for line in lines] == ["py.warnings", "uvicorn.access"]
    assert "deprecated knob" in lines[0]["message"]
    assert lines[1]["message"].endswith('"POST /orders?cartId=1 HTTP/1.1" 200')


@pytest.mark.usefixtures("restore_logging_state")
def test_configure_logging_fails_fast_without_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(KeyError, match="GOOGLE_CLOUD_PROJECT"):
        configure_logging()
