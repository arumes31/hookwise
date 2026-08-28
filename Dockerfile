# Stage 1: Build
FROM python:3.13-slim@sha256:7ce4b6dfe35e55397b7cda544f8a13f191b7ae28dc5aad71fe664dbc9bc2623f AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc=4:14.2.0-1 \
    python3-dev=3.13.5-1 \
    libpq-dev=17.11-0+deb13u1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --prefix=/install --requirement requirements.txt

# Stage 2: Runtime
FROM python:3.13-slim@sha256:7ce4b6dfe35e55397b7cda544f8a13f191b7ae28dc5aad71fe664dbc9bc2623f AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5=17.11-0+deb13u1 \
    postgresql-client=17+278 \
    netcat-openbsd=1.229-1 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m appuser && mkdir -p /app/data && chown -R appuser /app
COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser . .

# Remove unnecessary files from production image
RUN rm -rf tests .venv .git .pytest_cache .qodo

# Copy and set entrypoint (as root)
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=3).close()"]

# Switch to non-root user
USER appuser

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "--worker-class", "gevent", "--workers", "1", "--timeout", "420", "--bind", "0.0.0.0:5000", "app:app"]
