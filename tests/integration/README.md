# Integration tests

These tests run against the real Compose stack (proxy, worker, beat, migrations,
Postgres, Redis). ConnectWise is replaced by `cw_mock.py`; the overlay pins
`CW_URL` to the mock, so no real tickets can be created. CI runs them in
`.github/workflows/integration.yml`.

- `test_stack.py`: a heartbeat through ingress and worker, then a stale check
  through the broker. A rejected and an accepted stale ticket must both show up
  in history, audit, endpoint state and the run summary. Beat and worker must
  report healthy.
- `test_browser_roles.py`: signs in as viewer, operator and admin, opens every
  page the navigation offers and fails on any 4xx/5xx response or script error.
  Checks the maintenance switch for admins and the read-only view for viewers.

`cw_mock.py` accepts tickets only for the company `ACME` and lists every
request at `GET /__requests`.

## Running locally

Uses the values from `.env` for everything except ConnectWise. Pick a free
port if the regular dev stack already uses 5000.

```bash
python -m pip install --require-hashes -r tests/integration/requirements.txt
python -m playwright install chromium

export COMPOSE_PROJECT_NAME=hookwise-it HOOKWISE_IT_PORT=5077
export COMPOSE_FILE=docker-compose.yml:tests/integration/docker-compose.integration.yml  # use ; on Windows
docker compose up -d --build --wait

HOOKWISE_IT_BASE_URL=http://localhost:5077 pytest tests/integration -v

docker compose down -v
```

Without `HOOKWISE_IT_BASE_URL` the tests are skipped, so the regular
`pytest tests/` run is unaffected.
