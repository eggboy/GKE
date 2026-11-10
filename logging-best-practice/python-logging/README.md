# python-logging: Cloud Logging JSON for FastAPI on GKE

A FastAPI service whose logs suit Cloud Logging on GKE. Each line the service writes to stdout is one JSON document that GKE's logging agent turns into a `LogEntry` with `severity`, `timestamp`, `sourceLocation`, `labels`, and the `trace` / `spanId` / `traceSampled` fields that join it to the Cloud Trace waterfall. A traceback stays inside its line.

Two lines from `kubectl logs` on a GKE cluster under `opentelemetry-instrument`, with the project id replaced and the second traceback shortened:

```json
{"timestamp": "2026-11-10T07:26:59.969058Z", "severity": "INFO", "message": "placed order", "logger": "python_logging.main", "timestampSeconds": 1794295619, "timestampNanos": 969058560, "logging.googleapis.com/sourceLocation": {"file": "/app/.venv/lib/python3.12/site-packages/python_logging/main.py", "line": 93, "function": "place_order"}, "logging.googleapis.com/trace": "projects/PROJECT_ID/traces/7d1f0e5a9c3b4a6e8f2d1c0b9a8e7fa1", "logging.googleapis.com/spanId": "df2035addf1d8e9f", "logging.googleapis.com/trace_sampled": true, "order_id": "ord-11a1e1ac", "cart_id": "cart-1001", "total": 67.97}
{"timestamp": "2026-11-10T07:27:00.698363Z", "severity": "ERROR", "message": "request failed\nTraceback (most recent call last):\n  File \"/app/.venv/lib/python3.12/site-packages/python_logging/main.py\", line 104, in boom\n    _read_payment_gateway()\n ...\nTimeoutError: read timed out\n\nThe above exception was the direct cause of the following exception:\n\nTraceback (most recent call last):\n ...\nRuntimeError: payment gateway timed out", "logger": "python_logging.main", "timestampSeconds": 1794295620, "timestampNanos": 698363392, "logging.googleapis.com/sourceLocation": {"file": "/app/.venv/lib/python3.12/site-packages/python_logging/main.py", "line": 62, "function": "log_unhandled_exceptions"}, "logging.googleapis.com/trace": "projects/PROJECT_ID/traces/7d1f0e5a9c3b4a6e8f2d1c0b9a8e7fa2", "logging.googleapis.com/spanId": "e9c3bd6d30337539", "logging.googleapis.com/trace_sampled": true}
```

On a laptop, without `opentelemetry-instrument`, the lines lack `logging.googleapis.com/trace`, `/spanId` and `/trace_sampled`.

## What is in here

| File | Role |
| :--- | :--- |
| [`structured_logging.py`](src/python_logging/structured_logging.py) | `CloudLoggingJsonFormatter`, the `uvicorn.access` filter for health probes, and `configure_logging()`, which routes all loggers (uvicorn's and `warnings` included) through one handler on stdout. The file to copy into your own service. |
| [`main.py`](src/python_logging/main.py) | Three endpoints to see a line with fields, a 404 and a 500 with a chained traceback, plus the app-wide dependency that logs an unhandled exception while the span is still open. |
| [`test_structured_logging.py`](tests/test_structured_logging.py), [`test_main.py`](tests/test_main.py) | 35 tests; they parse each captured line with a JSON parser that rejects `NaN`. |
| [`Dockerfile`](Dockerfile) | `uv`-built image that starts `opentelemetry-instrument uvicorn ...` with nothing but JSON on stdout and stderr. |
| [`k8s/python-logging.yaml`](k8s/python-logging.yaml) | `Instrumentation` CR plus Deployment and Service for the `glb-trace-adk` cluster. |

## Run it

Python 3.12, `uv`. `pyproject.toml` pins PyPI as the index so that `uv.lock` holds public URLs: Cloud Build builds the image and cannot reach a package proxy configured on your machine.

```bash
uv sync
uv run ruff check . && uv run pytest                           # 35 tests
GOOGLE_CLOUD_PROJECT=my-project OTEL_TRACES_EXPORTER=none OTEL_METRICS_EXPORTER=none \
  uv run opentelemetry-instrument uvicorn python_logging.main:app --port 8080
curl -X POST 'localhost:8080/orders?cartId=cart-1001'          # INFO line with order_id, cart_id, total
curl -X POST 'localhost:8080/orders?cartId=cart-9'             # 404, WARNING line with cart_id
curl localhost:8080/boom                                       # 500, ERROR line with the traceback in message
curl localhost:8080/nope                                       # 404, access line only
```

Without `GOOGLE_CLOUD_PROJECT`, `configure_logging()` raises `KeyError` at import: Cloud Logging links a log entry to its trace when the trace field carries the full resource name `projects/<id>/traces/<trace_id>`, so a missing project would break correlation without warning. On a laptop, set any value.

On the cluster, `scripts/02-build-deploy-gke.sh` in `glb-trace-adk` is the model: build the image with Cloud Build, `kubectl apply -f k8s/python-logging.yaml`, then `gcloud logging read 'trace="projects/PROJECT_ID/traces/<id>"'` shows the entries next to the load balancer entries.

`configure_logging()` takes `level`, `stream`, `quiet_access_paths` (default `("/healthz",)`) and `formatter`. `CloudLoggingJsonFormatter` takes `message_max_length` (default 65,536), `traceback_max_length` (default 131,072) and `source_location` (default `True`).

## What goes into each line

| Field | Source |
| :--- | :--- |
| `timestamp` | `record.created` as RFC 3339 with microseconds and `Z`. For people reading `kubectl logs`; GKE leaves it in `jsonPayload.timestamp` |
| `timestampSeconds`, `timestampNanos` | the same instant as two integers. GKE's agent makes them the entry's timestamp and removes them from `jsonPayload`, so the entry carries the time of the event |
| `severity` | `CRITICAL`, `ERROR`, `WARNING`, `INFO` or `DEBUG` by level number, so a custom level 25 is `INFO` instead of `Level 25` |
| `message` | `record.getMessage()`, capped, then `\n` and the traceback (`exc_info`) and the stack (`stack_info`) when present |
| `logger` | `record.name` |
| `logging.googleapis.com/sourceLocation` | `record.pathname`, `lineno`, `funcName`; `source_location=False` leaves it out |
| `logging.googleapis.com/trace`, `/spanId`, `/trace_sampled` | the `otelTraceID` / `otelSpanID` / `otelTraceSampled` attributes that `opentelemetry-instrumentation-logging` stamps on the record at creation time, when they hold a valid id; otherwise the current span at format time |
| `logging.googleapis.com/labels` | `extra={"logging.googleapis.com/labels": {...}}`; values become strings and GKE moves the object to `LogEntry.labels` |
| every other top-level key | `extra={...}` entries, and anything else a log record factory adds |
| names `extra` cannot use | the fields above, `stream` (GKE removes it), `time` (another timestamp spelling), `log_format_error`, keys starting with `_`, any other `logging.googleapis.com/*` key, and `color_message` (uvicorn's ANSI copy) |
| values | JSON types as they are, UTF-8 unescaped; `nan`, `inf` and `-inf` as strings because bare they are not JSON; mapping keys as strings; anything else through `str()` |
| `log_format_error` | only on a reduced line: the exception that stopped the formatter from writing the full line, next to `message` (the raw template) and `log_args` |

## The traceback caveat, and why the JSON looks like this

A Python traceback printed as text is 10 to 80 lines. The container runtime records one line at a time, so without structured logging the agent turns each `File "..."` line into its own `LogEntry`, at stdout's default severity and without the trace ID. The Kubernetes community settled on one answer: one JSON object per event, with the traceback as an escaped string inside it. Fluent Bit's built-in `python` multiline parser exists for the case where you own the agent configuration, which you do not on GKE.

Consequences handled in the formatter:

- **A JSON line above 256 KiB does not arrive as a structured entry.** On the test cluster a 300 KB line arrived as two plain-text entries at `INFO` and a 200 KB line arrived intact. `message_max_length` caps the message; `traceback_max_length` cuts the middle out of a long traceback so that the `Traceback (most recent call last):` header and the final exception line, the two parts Error Reporting reads, both survive. The formatter does not bound `extra` values; keep request bodies out of them.
- **Error Reporting parses the traceback out of `message`** when it is in Python's own format. A message line in front of it (`request failed\nTraceback ...`) keeps the log message as the error's title.
- **The formatter still writes a line for a record it cannot render.** The standard library's answer to a `%s` short of an argument is a 26-line plain-text traceback on stderr (`--- Logging error ---`) and no line on stdout. The formatter writes timestamp, severity, logger, the raw template and `log_format_error` instead.

## Checked on a live cluster

GKE `v1.37.1-gke.3020000` with Managed OpenTelemetry, 10 November 2026. A throwaway pod wrote hand-made lines to stdout:

| Line on stdout | What arrived in Cloud Logging |
| :--- | :--- |
| JSON with `severity`, `message`, extra keys | `jsonPayload`, entry severity set |
| `"ratio":NaN` (bare) | one `textPayload` entry at `INFO`; severity and fields lost |
| `"ratio":"NaN"` | `jsonPayload` |
| one key twice | `jsonPayload`, last value kept (the documentation calls it unsupported) |
| 300 KB JSON line | two `textPayload` entries at `INFO`, 200,000 and 37,921 characters |
| 200 KB JSON line | `jsonPayload`, intact |
| `timestamp` as RFC 3339 string | stays in `jsonPayload.timestamp`; the agent stamps the entry with the time the runtime received the line |
| `time` as RFC 3339 string | stays in `jsonPayload.time` |
| `timestampSeconds` and `timestampNanos` | become the entry's timestamp and leave `jsonPayload` |
| `logging.googleapis.com/labels` object | `LogEntry.labels` |
| `stream` | removed from `jsonPayload` |

The sample itself, called with a `traceparent` header:

| Request | Status | Entries under `trace="projects/.../traces/<id>"` |
| :--- | :--- | :--- |
| `POST /orders?cartId=cart-1001`, sampled | 200 | `INFO` `placed order` with `spanId`, `traceSampled: true`, `sourceLocation`, `order_id`, `cart_id`, `total`, plus the `INFO` access line |
| same, unsampled | 200 | the same two, `traceSampled` false |
| `GET /boom` | 500 | `ERROR` `request failed` with the chained traceback in `message` and the trace attached, plus the access line |
| `POST /orders?cartId=cart-9` | 404 | `WARNING` `unknown cart` with `cart_id`, plus the access line |
| `GET /nope` | 404 | the access line only |
| `POST /orders` without `cartId` | 422 | the access line only |
| `GET /healthz` | 200 | none |

Entry timestamps equal `jsonPayload.timestamp` (the entry keeps the nanoseconds from the pair, the string stops at microseconds), and the pod has written no plain-text entry.

## Traps and their fixes

| Trap | Effect | Fix |
| :--- | :--- | :--- |
| uvicorn's own log config | startup and error lines as plain text on stderr, which GKE ingests as `ERROR` | `configure_logging()` clears the `uvicorn*` handlers and re-enables propagation |
| One access record per readiness probe | the kubelet and the load balancer probe every few seconds: about 70,000 `INFO` entries per pod per day, most of the log | `AccessLogPathFilter` on `uvicorn.access`; `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS=healthz` does the same for spans |
| An exception without a handler | uvicorn logs it after the OpenTelemetry middleware has ended the span: an `ERROR` entry with no trace fields | the app-wide dependency in `main.py` logs it inside the span and answers 500 |
| `warnings.warn()` | plain text on stderr | `logging.captureWarnings(True)` |
| `print()` | plain text, and block-buffered in a container | `PYTHONUNBUFFERED=1` for the timing; the text itself needs a code review |
| Non-ASCII in a message | `\uc11c\uc6b8` escapes with the default `ensure_ascii`, four times the bytes and unreadable in `kubectl logs` | the formatter writes UTF-8; `PYTHONUTF8=1` makes sure stdout accepts it |
| `QueueHandler` / `QueueListener` | the formatter runs on the listener thread, where `trace.get_current_span()` is empty | `opentelemetry-instrumentation-logging` stamps the trace on the record at creation; the formatter reads those attributes first |
| `OTEL_PYTHON_LOG_CORRELATION=true` | the logging instrumentation calls `logging.basicConfig()` with a plain-text format | left unset |
| OTLP log export | each line ingested twice | off by default in Python (`OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED`); `logger_provider: null` in the CR keeps it off |
| `extra` named `timestampSeconds`, `time`, `stream` or `logging.googleapis.com/trace` | moved timestamp, dropped key, or a forged trace | the formatter filters reserved names and the `logging.googleapis.com/` prefix out of `extra` |

## References

- Python `logging` [HOWTO](https://docs.python.org/3/howto/logging.html) and [`Handler.handleError`](https://docs.python.org/3/library/logging.html#logging.Handler.handleError)
- Cloud Logging, [structured logging special fields](https://cloud.google.com/logging/docs/structured-logging) and [quotas](https://cloud.google.com/logging/quotas)
- GKE, [about GKE logs](https://cloud.google.com/kubernetes-engine/docs/concepts/about-logs)
- Error Reporting, [formatting error messages](https://cloud.google.com/error-reporting/docs/formatting-error-messages)
- OpenTelemetry Python, [logging instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/logging/logging.html) and [zero-code instrumentation](https://opentelemetry.io/docs/zero-code/python/)
- FastAPI, [dependencies with `yield`](https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/)
