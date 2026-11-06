# syntax=docker/dockerfile:1
FROM python:3.11-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
COPY mcp_server ./mcp_server
RUN pip install --prefix=/install ".[mcp]"

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HA_DB_PATH=/app/var/hospital.db \
    HA_ENV=prod

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --create-home app

COPY --from=build /install /usr/local
WORKDIR /app
COPY --chown=app:app src ./src
COPY --chown=app:app mcp_server ./mcp_server
RUN mkdir -p /app/var && chown -R app:app /app/var

USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"

# One worker on SQLite. Scale by running the API read-heavy and moving the store
# to Postgres, not by pointing several workers at one SQLite file.
CMD ["uvicorn", "hospital_agent.api.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
