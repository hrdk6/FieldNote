# FieldNote: slim, non-root image. Default command serves the dashboard on :8501.
# Stage 1 builds the web UI into src/fieldnote/web/static; stage 2 is the Python runtime.
FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web ./
RUN npm run build -- --outDir /static --emptyOutDir

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FIELDNOTE_DATA_DIR=/app/data \
    FIELDNOTE_OUT_DIR=/app/out

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY --from=web /static ./src/fieldnote/web/static
RUN pip install ".[postgres]"

COPY workspaces ./workspaces
COPY fixtures ./fixtures
COPY scripts ./scripts
COPY docs ./docs

RUN useradd --create-home --uid 10001 fieldnote \
    && mkdir -p /app/data /app/out \
    && chown -R fieldnote:fieldnote /app
USER fieldnote

EXPOSE 8501
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/api/meta', timeout=4)" || exit 1

CMD ["fieldnote", "dashboard", "--host", "0.0.0.0", "--port", "8501"]
