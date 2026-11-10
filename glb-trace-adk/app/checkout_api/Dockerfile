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
# OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_PROTOCOL and OTEL_TRACES_SAMPLER* are injected by the GKE
# Instrumentation CR and take precedence over these image defaults, which only matter for plain `docker run`.
ENV PATH="/app/.venv/bin:${PATH}" \
    OTEL_SERVICE_NAME="checkout-api" \
    OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf" \
    OTEL_PYTHON_FASTAPI_EXCLUDED_URLS="healthz"
USER appuser
EXPOSE 8080
# opentelemetry-instrument builds the OpenTelemetry SDK from OTEL_* and instruments FastAPI before the app is imported.
CMD ["opentelemetry-instrument", "uvicorn", "checkout_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
