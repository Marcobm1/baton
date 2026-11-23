FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BATON_DATA_DIR=/data \
    CODEX_HOME=/codex \
    BATON_USE_GH_CLI=0

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY baton ./baton
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 baton \
    && mkdir -p /data && chown baton /data

USER baton
EXPOSE 8787
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz')"
CMD ["baton", "serve", "--host", "0.0.0.0", "--port", "8787"]
