# Stage 1: Build
FROM python:3.14-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Runtime
FROM python:3.14-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m appuser && mkdir -p /app/data && chown -R appuser /app
USER appuser

COPY --from=builder /root/.local /home/appuser/.local
ENV PATH="/home/appuser/.local/bin:${PATH}"

COPY --chown=appuser:appuser . .

# Remove unnecessary files from production image
RUN rm -rf tests .venv .git .pytest_cache .qodo

EXPOSE 5000

CMD ["gunicorn", "--worker-class", "eventlet", "--workers", "1", "--bind", "0.0.0.0:5000", "app:app"]
