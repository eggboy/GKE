r"""Cloud Logging structured JSON on stdout, correlated with the active OpenTelemetry span.

GKE's logging agent parses single-line JSON written to stdout and moves the ``logging.googleapis.com/*`` keys onto the
LogEntry (``trace``, ``spanId``, ``traceSampled``, ``sourceLocation``, ``labels``). Every line written while a request
span is active therefore shows up next to the load balancer and application spans in Trace Explorer and under
``trace="projects/<id>/traces/<trace_id>"`` in Logs Explorer. Application logs are not exported through the OTLP
collector; this stdout path is the only one.

The agent takes the entry's timestamp from ``timestampSeconds`` and ``timestampNanos`` and removes both from the
payload. It does not read the RFC 3339 ``timestamp`` string; without the numeric pair it stamps the entry with the
moment the container runtime received the line. The formatter writes both forms: the pair for the agent, the string
for whoever reads ``kubectl logs``.

The formatter also covers four gaps in the standard library:

* ``Handler.handleError`` prints a multi-line traceback to stderr when formatting fails (a ``%s`` without its
  argument is enough), and the line itself is lost. :meth:`CloudLoggingJsonFormatter.format` never raises; it writes
  a reduced line with ``log_format_error`` instead, and that line keeps the trace of its request.
* ``json.dumps`` writes ``NaN`` and ``Infinity`` bare, which is not JSON and turns the whole line into a text entry.
  The formatter writes them as strings.
* A lone surrogate (``json.loads('"\ud800"')`` returns one) cannot be encoded as UTF-8, so the stream raises and the
  line is lost. The formatter writes ``?`` in its place.
* Nothing bounds the message or the traceback, and an entry above 256 KiB is lost: on GKE a line of 262,144 bytes
  never arrived and a longer one arrived as plain text. Both get a cap in characters, which holds a line of ASCII
  under 197 KB. The caps do not count bytes, and ``extra`` values have no cap: a line of Korean text that long, or
  a large ``extra``, goes over the limit.
"""

import contextlib
import copy
import json
import logging
import logging.handlers
import math
import os
import sys
import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from types import TracebackType
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
#: Present only on a reduced line whose message could not be rendered: the arguments of the logging call.
FORMAT_ARGS = "log_args"

DEFAULT_MESSAGE_MAX_LENGTH = 65_536
DEFAULT_TRACEBACK_MAX_LENGTH = 131_072
DEFAULT_QUIET_ACCESS_PATHS = ("/healthz",)

_GOOGLE_PREFIX = "logging.googleapis.com/"
_NANOS_PER_SECOND = 1_000_000_000
_TRACE_ID_HEX_LENGTH = 32
_SPAN_ID_HEX_LENGTH = 16
# Set on every record by the factory that configure_logging() installs: (trace id, span id, sampled) of the span that
# was current when the record was created, or None when there was none. The leading underscore keeps it out of the
# user fields.
_TRACE_CONTEXT_ATTR = "_trace_context"
_MISSING = object()

# Standard LogRecord attributes plus the ones Formatter adds, uvicorn's ANSI duplicate of the message, and the
# attributes opentelemetry-instrumentation-logging adds when someone installs it with OTEL_PYTHON_LOG_CORRELATION:
# none of these are user ``extra`` fields.
_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "color_message",
    "otelTraceID",
    "otelSpanID",
    "otelTraceSampled",
    "otelServiceName",
}
# Names an ``extra`` may not use: the members written here, ``stream`` (GKE's agent removes it from the payload)
# and ``time`` (another spelling of the entry timestamp the agent accepts).
_RESERVED = frozenset(
    {
        "timestamp",
        TIMESTAMP_SECONDS,
        TIMESTAMP_NANOS,
        "time",
        "severity",
        "message",
        "logger",
        "stream",
        FORMAT_ERROR,
        FORMAT_ARGS,
    }
)
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

logger = logging.getLogger(__name__)


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

    def _format(self, record: logging.LogRecord) -> str:
        payload = self._core(record, self._message(record))
        if self._source_location:
            payload[SOURCE_LOCATION] = {"file": record.pathname, "line": record.lineno, "function": record.funcName}
        self._add_trace(payload, record)
        labels = _labels(record)
        if labels:
            payload[LABELS] = labels
        # _user_fields drops reserved names and logging.googleapis.com/* keys, so nothing above gets overwritten.
        payload.update(_user_fields(record))
        return _encode(payload)

    def _fallback(self, record: logging.LogRecord, error: Exception) -> str:
        payload = self._core(record, _truncate(_safe_text(record.msg), self._message_max_length))
        # The reduced line stays findable from its request; nothing that reads the trace may cost the line.
        with contextlib.suppress(Exception):
            self._add_trace(payload, record)
        payload[FORMAT_ERROR] = f"{type(error).__name__}: {_safe_text(error)}"
        if record.args:
            payload[FORMAT_ARGS] = _safe_text(record.args)
        return _encode(payload)

    def _core(self, record: logging.LogRecord, message: str) -> dict[str, Any]:
        """Members of every line, reduced ones included."""
        iso, seconds, nanos = _timestamps(record.created)
        return {
            "timestamp": iso,
            "severity": _severity(record.levelno),
            "message": message,
            "logger": record.name,
            TIMESTAMP_SECONDS: seconds,
            TIMESTAMP_NANOS: nanos,
        }

    def _add_trace(self, payload: dict[str, Any], record: logging.LogRecord) -> None:
        context = _trace_context(record)
        if context is not None:
            trace_id, span_id, sampled = context
            payload[TRACE] = f"{self._trace_prefix}{trace_id}"
            payload[SPAN_ID] = span_id
            payload[TRACE_SAMPLED] = sampled

    def _message(self, record: logging.LogRecord) -> str:
        """Join the capped message with the traceback and the stack, each cut in the middle when over its cap."""
        parts = [_truncate(record.getMessage(), self._message_max_length)]
        if record.exc_info and not record.exc_text:
            # Cached on the record, the way logging.Formatter does it.
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            parts.append(_clip_middle(record.exc_text, self._traceback_max_length))
        if record.stack_info:
            parts.append(_clip_middle(self.formatStack(record.stack_info), self._traceback_max_length))
        return "\n".join(part for part in parts if part)


class AccessLogPathFilter(logging.Filter):
    """Keep query strings out of uvicorn's access records and drop the records of successful health probes.

    uvicorn logs one record per request through ``uvicorn.access`` with the arguments
    ``(client, method, path_with_query, http_version, status_code)``. Readiness probes from the kubelet and the load
    balancer arrive several times a second and would otherwise be most of the log; a probe that fails (status 400 and
    above) is kept, because that record says why a Pod left the load balancer. The query string is cut from every
    record that is kept: it is where tokens and personal data travel, and the path is enough to tell requests apart.
    """

    def __init__(self, paths: Iterable[str] = ()) -> None:
        """Keep the paths whose successful requests are dropped, compared without their query string."""
        super().__init__()
        self._paths = frozenset(paths)

    def filter(self, record: logging.LogRecord) -> bool:
        """Return ``False`` for a successful request to a quiet path; rewrite the others without their query."""
        args = record.args
        if not (isinstance(args, tuple) and len(args) == 5):
            return True
        client, method, target, http_version, status = args
        path = str(target).partition("?")[0]
        if path in self._paths and isinstance(status, int) and status < 400:
            return False
        record.args = (client, method, path, http_version, status)
        return True


class StructuredQueueHandler(logging.handlers.QueueHandler):
    """Hand records to a ``QueueListener`` as they are, so the JSON formatter on the listener does all the rendering.

    ``QueueHandler.prepare()`` formats each record on the logging thread with a plain ``logging.Formatter`` before it
    queues it. Two protections of :class:`CloudLoggingJsonFormatter` are lost that way: a ``%s`` without its argument
    raises there, the record is dropped and a traceback goes to stderr; and the traceback is merged into the message,
    where the message cap cuts its last lines, the exception itself included. This handler queues a copy of the
    untouched record instead.

    Use it with an in-process queue (``queue.Queue``, ``queue.SimpleQueue``). The record keeps its arguments and its
    exception, which a ``multiprocessing`` queue could not pickle.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """Return a copy, so that handlers on the logging thread and the listener do not share one record."""
        return copy.copy(record)


def configure_logging(
    level: int = logging.INFO,
    *,
    stream: TextIO | None = None,
    quiet_access_paths: Iterable[str] = DEFAULT_QUIET_ACCESS_PATHS,
    formatter: logging.Formatter | None = None,
) -> None:
    """Send every logger, including uvicorn's, the warnings module and uncaught exceptions, through one JSON handler.

    Args:
        level: Root logger level.
        stream: Destination of the lines; ``sys.stdout`` when omitted. Tests pass a buffer.
        quiet_access_paths: Request paths whose successful uvicorn access records are dropped.
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
    # This also removes the OTLP LoggingHandler that opentelemetry-instrumentation-logging, when someone installs it,
    # puts on the root logger by default: stdout is the only log path.
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    install_trace_record_factory()
    # uvicorn installs its own plain-text handlers (startup and error lines on stderr, which GKE ingests with
    # severity ERROR) and disables propagation. Undo both so its lines are JSON and trace-correlated like the rest.
    access = logging.getLogger("uvicorn.access")
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        if uvicorn_logger is access and _access_log_switched_off(access):
            continue
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    access.filters = [existing for existing in access.filters if not isinstance(existing, AccessLogPathFilter)]
    access.addFilter(AccessLogPathFilter(quiet_access_paths))
    # httpx logs one INFO line per outbound request, which only duplicates the client span.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # warnings.warn() would otherwise print plain text to stderr.
    logging.captureWarnings(True)
    # So would an exception nothing catches, on the main thread or on any other.
    sys.excepthook = _log_uncaught_exception
    threading.excepthook = _log_uncaught_thread_exception


def install_trace_record_factory() -> None:
    """Stamp every log record, when it is created, with the span that is current on the logging thread.

    A formatter behind a ``QueueHandler`` runs on the listener thread, where ``trace.get_current_span()`` sees no span
    at all. The record factory runs inside ``Logger.makeRecord``, on the thread and in the context of the logging
    call, so the stamp is the request's span wherever the record is formatted later. Calling this twice installs the
    factory once.
    """
    current = logging.getLogRecordFactory()
    if getattr(current, "stamps_trace_context", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = current(*args, **kwargs)
        record.__dict__[_TRACE_CONTEXT_ATTR] = _current_trace_context()
        return record

    factory.stamps_trace_context = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


def _access_log_switched_off(access: logging.Logger) -> bool:
    """Tell whether uvicorn ran with ``--no-access-log``: it leaves the logger without handlers and propagation."""
    return not access.handlers and not access.propagate


def _log_uncaught_exception(
    exc_type: type[BaseException], exc_value: BaseException, exc_traceback: TracebackType | None
) -> None:
    """``sys.excepthook``: the traceback of a crash goes into one CRITICAL line instead of plain text on stderr."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


def _log_uncaught_thread_exception(args: threading.ExceptHookArgs) -> None:
    """``threading.excepthook``: same for a thread that dies; like the default hook it ignores ``SystemExit``."""
    if issubclass(args.exc_type, SystemExit):
        return
    name = args.thread.name if args.thread is not None else "unknown"
    logger.error("uncaught exception in thread %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


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


def _trace_context(record: logging.LogRecord) -> tuple[str, str, bool] | None:
    """Trace id, span id and sampled flag of the record's span.

    The stamp from :func:`install_trace_record_factory` decides when it is there, also when it says that no span was
    current. A record without a stamp (the factory is not installed) gets the span that is current at format time.
    """
    stamp = record.__dict__.get(_TRACE_CONTEXT_ATTR, _MISSING)
    if stamp is _MISSING:
        return _current_trace_context()
    if (
        isinstance(stamp, tuple)
        and len(stamp) == 3
        and _is_hex_id(stamp[0], _TRACE_ID_HEX_LENGTH)
        and _is_hex_id(stamp[1], _SPAN_ID_HEX_LENGTH)
    ):
        return stamp[0], stamp[1], bool(stamp[2])
    return None


def _current_trace_context() -> tuple[str, str, bool] | None:
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return f"{span_context.trace_id:032x}", f"{span_context.span_id:016x}", bool(span_context.trace_flags.sampled)


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


def _encode(payload: Mapping[str, Any]) -> str:
    """One JSON document, UTF-8 as is. A lone surrogate, which UTF-8 cannot carry, becomes ``?``."""
    line = json.dumps(payload, default=str, ensure_ascii=False)
    if line.isascii():
        return line
    try:
        line.encode()
    except UnicodeEncodeError:
        return line.encode("utf-8", "replace").decode()
    return line


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
