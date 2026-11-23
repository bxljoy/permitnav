# PermitNav API: the answering pipeline behind FastAPI, bge-m3 on the CPU (phase 3).
# The model weights are not baked in: they download once into the /models volume.
FROM python:3.15-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    HF_HOME=/models \
    PYTHONUNBUFFERED=1

# Dependencies first (cached while only the code changes). On Linux, torch is the CPU-only
# build (pyproject.toml, [tool.uv.sources]).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --extra embed --extra api --no-install-project

COPY src ./src
COPY migrations ./migrations
RUN uv sync --locked --no-dev --extra embed --extra api

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /models /app/data \
 && chown -R app:app /models /app/data
USER app
ENV PATH=/app/.venv/bin:$PATH

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=600s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# Migrations are idempotent; then serve. One worker: bge-m3 takes ~2–3 GB of memory.
CMD ["sh", "-c", "permitnav migrate && exec uvicorn permitnav.api:app --host 0.0.0.0 --port 8000 --workers 1"]
