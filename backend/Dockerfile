# Volta's backend. Runs on anything that runs a container.
#
# It is not a serverless app and cannot be one: main.py starts a loop that sweeps for missed
# delivery deadlines every 60 seconds, and Vapi expects an answer to assistant-request inside a
# hard, non-configurable 7.5 seconds -- a cold start spends that budget before any work begins.

FROM python:3.12-slim AS base

# uv, pinned by digest-free tag but installed from the official image so the version travels
# with the build rather than with whatever the network served that day.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so a code change does not re-resolve the world. --frozen makes the build
# fail on a stale lockfile instead of quietly resolving something the tests never ran against.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY backend/ ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Render, Railway and Fly all inject PORT. Defaulting keeps `docker run` working locally.
ENV PORT=8000
EXPOSE 8000

# One worker on purpose. The deadline sweep is a loop inside the process, and a second worker
# would run a second sweep -- idempotency keys make that harmless rather than double-dialling,
# but two processes racing for the same work is not a thing to arrange deliberately.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
