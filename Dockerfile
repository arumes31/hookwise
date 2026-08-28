# Stage 1: Build
FROM python:3.14.7-slim AS builder

WORKDIR /app

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.txt \
    && pip check

# Stage 2: Runtime
FROM python:3.14.7-slim AS runtime

WORKDIR /app

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m appuser && mkdir -p /app/data && chown -R appuser /app
COPY --from=builder /install /usr/local
# The base image's packaging tools and MessagePack are not needed at runtime.
# Removing them keeps the final image free of their fixed high-severity CVEs.
RUN python -m pip uninstall --yes msgpack setuptools
COPY --chown=appuser:appuser . .

# Copy and set entrypoint (as root)
COPY docker-entrypoint.sh /usr/local/bin/
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 5000

# Switch to non-root user
USER appuser

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "--worker-class", "gevent", "--workers", "1", "--timeout", "420", "--bind", "0.0.0.0:5000", "app:app"]
