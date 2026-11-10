"""Cloud Logging structured JSON on stdout, correlated with the active OpenTelemetry span.

GKE's logging agent parses single-line JSON written to stdout and moves the ``logging.googleapis.com/*`` keys onto the
LogEntry (``trace``, ``spanId``, ``traceSampled``, ``sourceLocation``, ``labels``). Every line written while a request
span is active therefore shows up next to the load balancer and application spans in Trace Explorer and under
``trace="projects/<id>/traces/<trace_id>"`` in Logs Explorer. Application logs are not exported through the OTLP
collector; this stdout path is the only one.

The agent takes the entry's timestamp from ``timestampSeconds`` and ``timestampNanos`` and removes both from the
payload. It does not read the RFC 3339 ``timestamp`` string; without the numeric pair it stamps the entry with the
moment the container runtime received the line. The formatter writes both forms: the pair for the agent, the string
for whoever reads ``kubectl logs``.

The formatter also covers three gaps in the standard library:

* ``Handler.handleError`` prints a multi-line traceback to stderr when formatting fails (a ``%s`` without its
  argument is enough), and the line itself is lost. :meth:`CloudLoggingJsonFormatter.format` never raises; it writes
  a reduced line with ``log_format_error`` instead.
* ``json.dumps`` writes ``NaN`` and ``Infinity`` bare, which is not JSON and turns the whole line into a text entry.
  The formatter writes them as strings.
* Nothing bounds the message or the traceback, and Cloud Logging stops treating a line as JSON somewhere above
  200 KB. Both get a cap.
"""

import json
import logging
import math
import os
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, TextIO

from opentelemetry import trace

SOURCE_LOCATION = "logging.googleapis.com/sourceLocation"
TRACE = "logging.googleapis.com/trace"
SPAN_ID = "logging.googleapis.com/spanId"
TRACE_SAMPLED = "logging.googleapis.com/trace_sampled"
LABELS = "logging.googleapis.com/labels"
TIMESTAMP_SECONDS = "timestampSeconds"
TIMESTAMP_NANOS = "timestampNanos"
#: Present only on a reduced line: why the full document could not be written.
FORMAT_ERROR = "log_format_error"

DEFAULT_MESSAGE_MAX_LENGTH = 65_536
DEFAULT_TRACEBACK_MAX_LENGTH = 131_072
DEFAULT_QUIET_ACCESS_PATHS = ("/healthz",)

_GOOGLE_PREFIX = "logging.googleapis.com/"
_NANOS_PER_SECOND = 1_000_000_000
_TRACE_ID_HEX_LENGTH = 32
_SPAN_ID_HEX_LENGTH = 16

# opentelemetry-instrumentation-logging stamps these on every record when it is created, on the logging thread.
# They are read for the trace members and never passed through as user fields.
_OTEL_TRACE_ID = "otelTraceID"
_OTEL_SPAN_ID = "otelSpanID"
_OTEL_TRACE_SAMPLED = "otelTraceSampled"
_OTEL_SERVICE_NAME = "otelServiceName"

# Standard LogRecord attributes plus the ones Formatter adds, uvicorn's ANSI duplicate of the message and the
# OpenTelemetry attributes above: none of these are user ``extra`` fields.
_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "color_message",
    _OTEL_TRACE_ID,
    _OTEL_SPAN_ID,
    _OTEL_TRACE_SAMPLED,
    _OTEL_SERVICE_NAME,
}
# Names an ``extra`` may not use: the members written here, ``stream`` (GKE's agent removes it from the payload)
# and ``time`` (another spelling of the entry timestamp the agent accepts).
_RESERVED = frozenset(
    {"timestamp", TIMESTAMP_SECONDS, TIMESTAMP_NANOS, "time", "severity", "message", "logger", "stream", FORMAT_ERROR}
)
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class CloudLoggingJsonFormatter(logging.Formatter):
    """Format records as single-line Cloud Logging JSON with trace correlation, tracebacks and ``extra`` fields."""

    def __init__(
        self,
        project_id: str,
        *,
        message_max_length: int = DEFAULT_MESSAGE_MAX_LENGTH,
        traceback_max_length: int = DEFAULT_TRACEBACK_MAX_LENGTH,
        source_location: bool = True,
    ) -> None:
        """Remember the project and the limits.

        Args:
            project_id: Google Cloud project, needed because Cloud Logging only links a log entry to its trace when
                the trace field is the full resource name ``projects/<id>/traces/<trace_id>``.
            message_max_length: Characters of the message kept before ``... [N more characters]``; 0 disables the cap.
                The traceback is not counted.
            traceback_max_length: Characters of the traceback kept; the middle is cut so that both the ``Traceback``
                header and the final exception line survive. 0 disables the cap.
            source_location: Whether to write ``logging.googleapis.com/sourceLocation``.

        """
        super().__init__()
        self._trace_prefix = f"projects/{project_id}/traces/"
        self._message_max_length = message_max_length
        self._traceback_max_length = traceback_max_length
        self._source_location = source_location

    def format(self, record: logging.LogRecord) -> str:
        """Render one record; when building the full line fails, write a reduced line instead of raising."""
        try:
            return self._format(record)
        except Exception as error:  # noqa: BLE001 - whatever broke, a degraded line beats a lost one
            return self._fallback(record, error)

    def formatException(self, ei: Any) -> str:  # noqa: N802 - logging.Formatter API
        """Render the traceback, cut in the middle when it exceeds the configured length."""
        return _clip_middle(super().formatException(ei), self._traceback_max_length)

    def _format(self, record: logging.LogRecord) -> str:
        iso, seconds, nanos = _timestamps(record.created)
        payload: dict[str, Any] = {
            "timestamp": iso,
            "severity": _severity(record.levelno),
            "message": self._message(record),
            "logger": record.name,
            TIMESTAMP_SECONDS: seconds,
            TIMESTAMP_NANOS: nanos,
        }
        if self._source_location:
            payload[SOURCE_LOCATION] = {"file": record.pathname, "line": record.lineno, "function": record.funcName}
        context = _trace_context(record)
        if context is not None:
            trace_id, span_id, sampled = context
            payload[TRACE] = f"{self._trace_prefix}{trace_id}"
            if span_id is not None:
                payload[SPAN_ID] = span_id
            payload[TRACE_SAMPLED] = sampled
        labels = _labels(record)
        if labels:
            payload[LABELS] = labels
        # _user_fields drops reserved names and logging.googleapis.com/* keys, so nothing above gets overwritten.
        payload.update(_user_fields(record))
        return json.dumps(payload, default=str, ensure_ascii=False)

    def _message(self, record: logging.LogRecord) -> str:
        message = _truncate(record.getMessage(), self._message_max_length)
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            message = f"{message}\n{record.exc_text}"
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"
        return message

    def _fallback(self, record: logging.LogRecord, error: Exception) -> str:
        iso, seconds, nanos = _timestamps(record.created)
        payload: dict[str, Any] = {
            "timestamp": iso,
            "severity": _severity(record.levelno),
            "message": _truncate(_safe_text(record.msg), self._message_max_length),
            "logger": record.name,
            TIMESTAMP_SECONDS: seconds,
            TIMESTAMP_NANOS: nanos,
            FORMAT_ERROR: f"{type(error).__name__}: {_safe_text(error)}",
        }
        if record.args:
            payload["log_args"] = _safe_text(record.args)
        return json.dumps(payload, ensure_ascii=False)


class AccessLogPathFilter(logging.Filter):
    """Drop uvicorn access records for the given request paths, the health probes in particular.

    uvicorn logs one record per request through ``uvicorn.access`` with the arguments
    ``(client, method, path_with_query, http_version, status_code)``. Readiness probes from the kubelet and the load
    balancer arrive several times a second and would otherwise be most of the log.
    """

    def __init__(self, paths: Iterable[str]) -> None:
        """Keep the paths to drop, compared without their query string."""
        super().__init__()
        self._paths = frozenset(paths)

    def filter(self, record: logging.LogRecord) -> bool:
        """Return ``False`` for an access record whose path is one of the quiet paths."""
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            return str(args[2]).partition("?")[0] not in self._paths
        return True


def configure_logging(
    level: int = logging.INFO,
    *,
    stream: TextIO | None = None,
    quiet_access_paths: Iterable[str] = DEFAULT_QUIET_ACCESS_PATHS,
    formatter: logging.Formatter | None = None,
) -> None:
    """Send every logger, including uvicorn's and the warnings module, through one JSON handler on stdout.

    Args:
        level: Root logger level.
        stream: Destination of the lines; ``sys.stdout`` when omitted. Tests pass a buffer.
        quiet_access_paths: Request paths whose uvicorn access records are dropped.
        formatter: Replacement for the default :class:`CloudLoggingJsonFormatter`, which reads the project from
            ``GOOGLE_CLOUD_PROJECT``.

    Raises:
        KeyError: if ``GOOGLE_CLOUD_PROJECT`` is unset and no formatter is given. Cloud Logging only links a log entry
            to its trace when the trace field is the full resource name ``projects/<id>/traces/<trace_id>``, so a
            missing project id would silently break correlation instead of failing here.

    """
    if formatter is None:
        formatter = CloudLoggingJsonFormatter(os.environ["GOOGLE_CLOUD_PROJECT"])
    handler = logging.StreamHandler(sys.stdout if stream is None else stream)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # uvicorn installs its own plain-text handlers (startup and error lines on stderr, which GKE ingests with
    # severity ERROR) and disables propagation. Undo both so its lines are JSON and trace-correlated like the rest.
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.filters = [existing for existing in access.filters if not isinstance(existing, AccessLogPathFilter)]
    quiet = frozenset(quiet_access_paths)
    if quiet:
        access.addFilter(AccessLogPathFilter(quiet))
    # httpx logs one INFO line per outbound request, which only duplicates the client span.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # warnings.warn() would otherwise print plain text to stderr.
    logging.captureWarnings(True)


def _severity(levelno: int) -> str:
    """Cloud Logging severity for a level number; custom levels map to the next standard one below them."""
    if levelno >= logging.CRITICAL:
        return "CRITICAL"
    if levelno >= logging.ERROR:
        return "ERROR"
    if levelno >= logging.WARNING:
        return "WARNING"
    if levelno >= logging.INFO:
        return "INFO"
    return "DEBUG"


def _timestamps(created: float) -> tuple[str, int, int]:
    """RFC 3339 string plus the (seconds, nanos) pair, all three derived from one nanosecond value."""
    seconds, nanos = divmod(round(created * _NANOS_PER_SECOND), _NANOS_PER_SECOND)
    moment = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanos // 1000)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z"), seconds, nanos


def _trace_context(record: logging.LogRecord) -> tuple[str, str | None, bool] | None:
    """Trace id, span id and sampled flag of the record's span.

    The attributes from opentelemetry-instrumentation-logging come first: the instrumentation captures them when it
    creates the record, so they stay correct behind a ``QueueHandler``, where this formatter runs on the listener
    thread and ``trace.get_current_span()`` sees no span at all.
    """
    trace_id = getattr(record, _OTEL_TRACE_ID, None)
    if _is_hex_id(trace_id, _TRACE_ID_HEX_LENGTH):
        span_id = getattr(record, _OTEL_SPAN_ID, None)
        return (
            trace_id,
            span_id if _is_hex_id(span_id, _SPAN_ID_HEX_LENGTH) else None,
            bool(getattr(record, _OTEL_TRACE_SAMPLED, False)),
        )
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        return f"{span_context.trace_id:032x}", f"{span_context.span_id:016x}", bool(span_context.trace_flags.sampled)
    return None


def _is_hex_id(value: object, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    try:
        return int(value, 16) != 0
    except ValueError:
        return False


def _labels(record: logging.LogRecord) -> dict[str, str]:
    """``extra={"logging.googleapis.com/labels": {...}}`` becomes ``LogEntry.labels``; label values are strings."""
    raw = record.__dict__.get(LABELS)
    if isinstance(raw, Mapping):
        return {str(key): str(value) for key, value in raw.items()}
    return {}


def _user_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {key: _json_safe(value) for key, value in record.__dict__.items() if _is_user_key(key)}


def _is_user_key(key: str) -> bool:
    return (
        key not in _RECORD_ATTRS
        and key not in _RESERVED
        and not key.startswith("_")
        and not key.startswith(_GOOGLE_PREFIX)
    )


def _json_safe(value: Any) -> Any:
    """JSON has no literal for NaN or Infinity; a string keeps the line parseable. Mapping keys become strings."""
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe(item) for item in value]
    return value


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}... [{len(text) - limit} more characters]"


def _clip_middle(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n... [{len(text) - limit} characters omitted] ...\n{text[-tail:]}"


def _safe_text(value: object) -> str:
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - the object's __str__ is the thing that is broken
        try:
            return repr(value)
        except Exception:  # noqa: BLE001
            return f"<unprintable {type(value).__name__}>"
