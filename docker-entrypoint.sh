#!/bin/sh
set -e

# Wait for Postgres
echo "Waiting for postgres..."
while ! nc -z hookwise-db 5432; do
  sleep 1
done
echo "Postgres started"

echo "Running migrations..."
if flask db upgrade; then
    echo "Migrations successful!"
else
    echo "Migrations failed!"
    exit 1
fi

echo "Starting Gunicorn..."
exec "$@"
