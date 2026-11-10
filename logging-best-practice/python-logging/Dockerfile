# syntax=docker/dockerfile:1
# Base images are pinned by multi-arch index digest; bump the tag and digest together.
# uv tag python3.12-bookworm-slim resolved on 2026-11-06.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Dependencies first (cache-friendly), exactly as locked; --locked fails the build if uv.lock is stale.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# python:3.12-slim-bookworm resolved on 2026-11-06.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser
COPY --from=builder /app/.venv /app/.venv
# OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_PROTOCOL and the sampler are injected by the GKE Instrumentation
# CR and take precedence over these image defaults, which only matter for plain `docker run`.
#   PYTHONUNBUFFERED=1                    a stray print() reaches the log agent when it happens, not when the 8 KiB
#                                         buffer fills; the logging handler flushes on its own either way
#   PYTHONUTF8=1                          the formatter writes UTF-8 as is; without a UTF-8 stdout a non-ASCII
#                                         character would raise inside the handler
#   OTEL_PYTHON_FASTAPI_EXCLUDED_URLS     no span per readiness probe; the access-log filter does the same for logs.
#                                         The value is a regex searched in scheme://host/path, so it is anchored:
#                                         a bare "healthz" also switches tracing off for /api/healthz-report and
#                                         for every request to a host named healthz-gw.example.com. (\$ is a
#                                         literal $ in a Dockerfile.)
#   OTEL_LOGS_EXPORTER=none               stdout JSON is the single log path; the distro would otherwise build an
#                                         OTLP log exporter
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    OTEL_SERVICE_NAME="python-logging" \
    OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf" \
    OTEL_PYTHON_FASTAPI_EXCLUDED_URLS="/healthz\$" \
    OTEL_LOGS_EXPORTER="none"
USER appuser
EXPOSE 8080
# opentelemetry-instrument builds the OpenTelemetry SDK from OTEL_* and activates the FastAPI instrumentation before
# the app is imported. No logging instrumentation is installed: configure_logging() stamps the trace on each record
# itself. If you add opentelemetry-instrumentation-logging, set OTEL_PYTHON_LOG_AUTO_INSTRUMENTATION=false, or it
# puts an OTLP log handler on the root logger.
CMD ["opentelemetry-instrument", "uvicorn", "python_logging.main:app", "--host", "0.0.0.0", "--port", "8080"]
