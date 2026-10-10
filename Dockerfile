# Stage 1: Build
FROM python:3.14.8-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder

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
FROM python:3.14.8-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime
# CI varies this value so cached builds still fetch current Debian security fixes.
ARG RUNTIME_APT_REFRESH=local

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
# These base-image packaging modules are not runtime dependencies. Remove every
# known vulnerable package location, pip's vendored dependencies, and the
# setuptools startup shim before copying app code.
RUN find /usr/local -depth \( \
    -name 'pip' -o \
    -name 'pip-*.dist-info' -o \
    -name 'pip3' -o \
    -name 'pip3.*' -o \
    -name 'msgpack*' -o \
    -name 'setuptools*' -o \
    -name '_distutils_hack' -o \
    -name 'distutils-precedence.pth' \
    \) -exec rm -rf {} +
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
