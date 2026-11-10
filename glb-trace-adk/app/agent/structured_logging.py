"""Cloud Logging structured JSON on stdout, correlated with the active OpenTelemetry span.

GKE's logging agent parses single-line JSON written to stdout and moves the ``logging.googleapis.com/*`` keys onto the
LogEntry (``trace``, ``spanId``, ``traceSampled``, ``sourceLocation``). Every line written while a request span is
active therefore shows up next to the load balancer and application spans in Trace Explorer and under
``trace="projects/<id>/traces/<trace_id>"`` in Logs Explorer. Application logs are not exported through the OTLP
collector; this stdout path is the only one.
"""

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

# Standard LogRecord attributes plus the ones Formatter adds, and uvicorn's ANSI duplicate of the message: none of
# these are user "extra" fields.
_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "color_message"}
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class CloudLoggingJsonFormatter(logging.Formatter):
    """Format records as single-line Cloud Logging JSON with trace correlation, tracebacks and ``extra`` fields."""

    def __init__(self, project_id: str) -> None:
        """Remember the project so the trace field can be the full ``projects/<id>/traces/<id>`` resource name."""
        super().__init__()
        self._trace_prefix = f"projects/{project_id}/traces/"

    def format(self, record: logging.LogRecord) -> str:
        """Render one record; ``extra={...}`` keys become top-level JSON fields, reserved keys always win."""
        message = record.getMessage()
        if record.exc_info:
            record.exc_text = record.exc_text or self.formatException(record.exc_info)
            message = f"{message}\n{record.exc_text}"
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"
        payload: dict[str, Any] = {
            key: value for key, value in record.__dict__.items() if key not in _RECORD_ATTRS and not key.startswith("_")
        }
        payload.update(
            {
                "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                "severity": record.levelname,
                "message": message,
                "logger": record.name,
                "logging.googleapis.com/sourceLocation": {
                    "file": record.pathname,
                    "line": record.lineno,
                    "function": record.funcName,
                },
            }
        )
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            payload["logging.googleapis.com/trace"] = f"{self._trace_prefix}{span_context.trace_id:032x}"
            payload["logging.googleapis.com/spanId"] = f"{span_context.span_id:016x}"
            payload["logging.googleapis.com/trace_sampled"] = bool(span_context.trace_flags.sampled)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Send every logger, including uvicorn's, through one JSON handler on stdout.

    Raises:
        KeyError: if ``GOOGLE_CLOUD_PROJECT`` is unset. Cloud Logging only links a log entry to its trace when the
            trace field is the full resource name ``projects/<id>/traces/<trace_id>``, so a missing project id would
            silently break correlation instead of failing here.

    """
    project_id = os.environ["GOOGLE_CLOUD_PROJECT"]
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudLoggingJsonFormatter(project_id))
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
    # httpx logs one INFO line per outbound request, which only duplicates the client span.
    logging.getLogger("httpx").setLevel(logging.WARNING)
