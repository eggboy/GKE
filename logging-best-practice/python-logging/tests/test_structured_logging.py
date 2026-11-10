"""Cloud Logging JSON formatter and root logging configuration."""

import contextlib
import io
import json
import logging
import logging.handlers
import queue
import sys
import threading
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
    StructuredQueueHandler,
    configure_logging,
    install_trace_record_factory,
)
from tests.support import PROJECT_ID, parse_line

EmitFn = Callable[..., str]


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


def _record(message: str) -> logging.LogRecord:
    """A record created here and now, through the record factory, without any handler involved."""
    named = logging.getLogger("tests.records")
    return named.makeRecord(named.name, logging.INFO, __file__, 1, message, (), None)


@pytest.mark.usefixtures("trace_stamps")
def test_trace_stamped_at_creation_wins_over_the_span_current_at_format_time() -> None:
    formatter = CloudLoggingJsonFormatter(PROJECT_ID)
    tracer = TracerProvider(sampler=ALWAYS_ON).get_tracer("tests")
    with tracer.start_as_current_span("request") as span:
        context = span.get_span_context()
        record = _record("stamped at creation")
    with tracer.start_as_current_span("unrelated"):
        payload = _payload(formatter.format(record) + "\n")

    assert payload[TRACE] == f"projects/{PROJECT_ID}/traces/{context.trace_id:032x}"
    assert payload[SPAN_ID] == f"{context.span_id:016x}"
    assert payload[TRACE_SAMPLED] is True
    assert not any(key.startswith("_") for key in payload)


@pytest.mark.usefixtures("trace_stamps")
def test_record_created_outside_a_span_has_no_trace_even_when_formatted_inside_one() -> None:
    formatter = CloudLoggingJsonFormatter(PROJECT_ID)
    record = _record("no span")
    with TracerProvider(sampler=ALWAYS_ON).get_tracer("tests").start_as_current_span("unrelated"):
        payload = _payload(formatter.format(record) + "\n")

    assert TRACE not in payload
    assert SPAN_ID not in payload


@pytest.mark.usefixtures("no_trace_stamps")
def test_record_without_a_stamp_gets_the_span_current_at_format_time(emit: EmitFn) -> None:
    with TracerProvider(sampler=ALWAYS_ON).get_tracer("tests").start_as_current_span("checkout") as span:
        context = span.get_span_context()
        payload = _payload(emit(logging.INFO, "formatter used on its own"))

    assert payload[TRACE] == f"projects/{PROJECT_ID}/traces/{context.trace_id:032x}"
    assert payload[SPAN_ID] == f"{context.span_id:016x}"


@pytest.mark.usefixtures("trace_stamps")
def test_trace_record_factory_is_installed_once() -> None:
    installed = logging.getLogRecordFactory()

    install_trace_record_factory()

    assert logging.getLogRecordFactory() is installed


def test_attributes_of_the_opentelemetry_logging_instrumentation_are_not_user_fields(emit: EmitFn) -> None:
    extra = {
        "otelTraceID": "4bf92f3577b34da6a3ce929d0e0e4736",
        "otelSpanID": "00f067aa0ba902b7",
        "otelServiceName": "x",
    }
    payload = _payload(emit(logging.INFO, "instrumentation installed as well", extra=extra))

    assert not any(key.startswith("otel") for key in payload)
    assert TRACE not in payload


@contextlib.contextmanager
def _queued_logger(
    handler_type: type[logging.handlers.QueueHandler], formatter: logging.Formatter | None = None
) -> Iterator[tuple[logging.Logger, io.StringIO]]:
    """A logger whose records cross a queue; the formatter under test runs on the listener thread."""
    stream = io.StringIO()
    target = logging.StreamHandler(stream)
    target.setFormatter(formatter or CloudLoggingJsonFormatter(PROJECT_ID))
    records: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    listener = logging.handlers.QueueListener(records, target)
    named = logging.getLogger("tests.queued")
    named.handlers = [handler_type(records)]
    named.propagate = False
    named.setLevel(logging.DEBUG)
    listener.start()
    try:
        yield named, stream
    finally:
        listener.stop()  # drains the queue: the stream is complete once the block is left
        named.handlers = []


@pytest.mark.usefixtures("trace_stamps")
@pytest.mark.parametrize("handler_type", [logging.handlers.QueueHandler, StructuredQueueHandler])
def test_queue_handlers_keep_the_trace_of_the_logging_thread(
    handler_type: type[logging.handlers.QueueHandler],
) -> None:
    tracer = TracerProvider(sampler=ALWAYS_ON).get_tracer("tests")
    with _queued_logger(handler_type) as (named, stream), tracer.start_as_current_span("checkout") as span:
        context = span.get_span_context()
        named.warning("formatted on the listener thread")
    payload = _payload(stream.getvalue())

    assert payload[TRACE] == f"projects/{PROJECT_ID}/traces/{context.trace_id:032x}"
    assert payload[SPAN_ID] == f"{context.span_id:016x}"
    assert payload[TRACE_SAMPLED] is True


@pytest.mark.usefixtures("quiet_capture_handler")
def test_structured_queue_handler_keeps_the_reduced_line_for_a_bad_format_string(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _queued_logger(StructuredQueueHandler) as (named, stream):
        named.info("value %s %s", "only-one")
    payload = _payload(stream.getvalue())

    assert payload["message"] == "value %s %s"
    assert payload[FORMAT_ERROR].startswith("TypeError: ")
    assert capsys.readouterr().err == "", "nothing may reach stderr"


def test_structured_queue_handler_leaves_the_traceback_to_the_json_formatter() -> None:
    formatter = CloudLoggingJsonFormatter(PROJECT_ID, message_max_length=20)
    with _queued_logger(StructuredQueueHandler, formatter) as (named, stream):
        try:
            raise ValueError("boom")
        except ValueError:
            named.exception("0123456789abcdefghijklmnopqrstuvwxyz")
    message = _payload(stream.getvalue())["message"]

    assert message.startswith("0123456789abcdefghij... [16 more characters]\nTraceback (most recent call last):")
    assert message.endswith("ValueError: boom"), "the message cap must not reach into the traceback"


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


def test_lone_surrogate_is_written_as_a_question_mark(capsys: pytest.CaptureFixture[str]) -> None:
    written = io.BytesIO()
    stream = io.TextIOWrapper(written, encoding="utf-8", errors="strict", write_through=True)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(CloudLoggingJsonFormatter(PROJECT_ID))
    named = logging.getLogger("tests.surrogates")
    named.handlers = [handler]
    named.propagate = False
    try:
        named.warning("name=%s", json.loads('"\\ud800"'), extra={"city": "\udc80 서울"})
    finally:
        named.handlers = []
    payload = _payload(written.getvalue().decode("utf-8"))

    assert payload["message"] == "name=?"
    assert payload["city"] == "? 서울"
    assert capsys.readouterr().err == "", "the stream must not refuse the line"


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


@pytest.mark.parametrize("broken", ["format string", "extra value"])
def test_reduced_line_keeps_the_trace_of_its_request(emit: EmitFn, broken: str) -> None:
    with TracerProvider(sampler=ALWAYS_ON).get_tracer("tests").start_as_current_span("checkout") as span:
        context = span.get_span_context()
        if broken == "format string":
            line = emit(logging.INFO, "value %s %s", "only-one")
        else:
            line = emit(logging.INFO, "still logged", extra={"bad": Explosive()})
    payload = _payload(line)

    assert FORMAT_ERROR in payload
    assert payload[TRACE] == f"projects/{PROJECT_ID}/traces/{context.trace_id:032x}"
    assert payload[SPAN_ID] == f"{context.span_id:016x}"
    assert payload[TRACE_SAMPLED] is True


def test_source_location_can_be_switched_off(emit: EmitFn) -> None:
    payload = _payload(emit(logging.INFO, "x", formatter=CloudLoggingJsonFormatter(PROJECT_ID, source_location=False)))

    assert SOURCE_LOCATION not in payload


def _access_record(path: str, status: int) -> logging.LogRecord:
    make = logging.getLogger("uvicorn.access").makeRecord
    return make(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("c", "GET", path, "1.1", status), None
    )


def test_access_log_filter_drops_successful_requests_to_the_quiet_paths_only() -> None:
    quiet = AccessLogPathFilter(["/healthz"])
    plain = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, "", 0, "plain %s", ("x",), None
    )

    assert quiet.filter(_access_record("/healthz", 200)) is False
    assert quiet.filter(_access_record("/healthz?verbose=1", 204)) is False
    assert quiet.filter(_access_record("/orders", 200)) is True
    assert quiet.filter(plain) is True


@pytest.mark.parametrize("status", [404, 500, 503])
def test_access_log_filter_keeps_a_failed_health_probe(status: int) -> None:
    record = _access_record("/healthz", status)

    assert AccessLogPathFilter(["/healthz"]).filter(record) is True
    assert record.getMessage() == f'c - "GET /healthz HTTP/1.1" {status}'


def test_access_log_filter_cuts_the_query_string() -> None:
    record = _access_record("/orders?cartId=cart-1001&access_token=review-canary", 200)

    assert AccessLogPathFilter().filter(record) is True
    assert record.getMessage() == 'c - "GET /orders HTTP/1.1" 200'


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
    assert lines[1]["message"].endswith('"POST /orders HTTP/1.1" 200')


@pytest.mark.usefixtures("restore_logging_state")
def test_configure_logging_leaves_an_access_log_that_uvicorn_switched_off() -> None:
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.handlers = []  # the state uvicorn --no-access-log leaves behind
    uvicorn_access.propagate = False
    buffer = io.StringIO()

    configure_logging(stream=buffer)
    uvicorn_access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:1", "POST", "/orders", "1.1", 200)

    assert uvicorn_access.hasHandlers() is False, "uvicorn asks this before it logs a request"
    assert buffer.getvalue() == ""


@pytest.mark.usefixtures("restore_logging_state")
def test_configure_logging_turns_uncaught_exceptions_into_json_lines(capsys: pytest.CaptureFixture[str]) -> None:
    buffer = io.StringIO()
    configure_logging(stream=buffer)

    def die() -> None:
        raise RuntimeError("worker died")

    try:
        raise RuntimeError("main died")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    worker = threading.Thread(target=die, name="worker-1")
    worker.start()
    worker.join()
    crashed, thread_died = (parse_line(line + "\n") for line in buffer.getvalue().splitlines())

    assert crashed["severity"] == "CRITICAL"
    assert crashed["message"].startswith("uncaught exception\nTraceback (most recent call last):")
    assert crashed["message"].endswith("RuntimeError: main died")
    assert thread_died["severity"] == "ERROR"
    assert thread_died["message"].startswith("uncaught exception in thread worker-1\nTraceback")
    assert thread_died["message"].endswith("RuntimeError: worker died")
    assert capsys.readouterr().err == "", "no plain-text traceback on stderr"


@pytest.mark.usefixtures("restore_logging_state")
def test_configure_logging_fails_fast_without_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(KeyError, match="GOOGLE_CLOUD_PROJECT"):
        configure_logging()
