# Base images are pinned by multi-arch index digest; bump the tag and digest together.
# uv tag python3.12-bookworm-slim resolved on 2026-11-06.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# The agent is not a package (no build-system), so uv sync installs only the locked dependencies into .venv.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

# python:3.12-slim-bookworm resolved on 2026-11-06.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser
COPY --from=builder /app/.venv /app/.venv
COPY main.py structured_logging.py ./
COPY agents ./agents
ENV PATH="/app/.venv/bin:${PATH}"
USER appuser
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
