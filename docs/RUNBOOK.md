# HookWise Operator Runbook

Practical procedures for running HookWise (Flask web + Celery worker/beat +
Redis + PostgreSQL, bridging webhooks to ConnectWise Manage tickets).

## Service map

| Component | Role | Health signal |
|-----------|------|---------------|
| `hookwise-proxy` | Flask web / API / dashboard | `GET /health`, `GET /health/services` |
| `hookwise-worker` | Celery worker — processes webhooks, creates/updates/closes tickets | `celery` field in `/health/services` |
| `hookwise-beat` | Celery beat — cleanup, timeout checks, health verification | scheduled task freshness |
| `redis` | Broker + metrics + maintenance flag | `redis` field in `/health/services` |
| `postgres` | Config, webhook logs, audit trail | `database` field in `/health/services` |
| `hookwise-llm` (optional) | Ollama for RCA / AI routing | `GET /api/health/llm` |

---

## Dead-letter queue (DLQ)

A webhook lands in the DLQ (`WebhookLog.status == "dlq"`) after Celery retries
are exhausted. DLQ items are **terminal failures** — they will not retry on
their own.

**Where to see it**
- Dashboard → *Ticket History* → **DLQ** stat card (today's count).
- Webhook History → filter **Status = DLQ** (`/history?status=dlq`).

**Triage**
1. Open the DLQ-filtered history; use **View** to inspect the payload and the
   `error_message`.
2. Common causes: invalid ConnectWise credentials, a board/status/priority that
   no longer exists, mapping gaps, or a CW outage during the retry window.
3. Fix the root cause (rotate keys, correct the endpoint's board/status, add the
   missing mapping), then **Replay** the item(s). Bulk-select to replay many.

**Escalate** if the DLQ count keeps climbing after a replay — that means the
underlying cause is unresolved (usually CW auth or a board rename).

---

## ConnectWise outage / elevated error rate

Symptoms: failures and DLQ growth, `create_ticket` errors in worker logs,
`ConnectWiseError` in `error_message`.

1. Confirm scope: is it auth (403/401 from CW) or availability (5xx/timeouts)?
   Check worker logs and, if present, the last CW error in each endpoint's
   Security/Health HUD.
2. If CW is down, enable **Maintenance Mode** (dashboard toggle, or
   `POST /admin/maintenance`). Inbound webhooks then get `503` and are not
   dropped by half-processing; sources that retry will re-deliver.
3. When CW recovers, disable Maintenance Mode and **replay** any DLQ items
   accumulated during the outage.
4. If a stale board/priority cache is suspected, clear it:
   `flask clear-cw-cache` (or the container's equivalent CLI entry).

---

## Bearer token / secret rotation

1. Dashboard → endpoint → **Rotate Bearer Token**. The old token stops working
   immediately.
2. Update the token in the source system's webhook configuration.
3. Verify with **Test Webhook**; confirm a `processed` entry in history.
4. For HMAC, update `hmac_secret` on the endpoint and the signing secret in the
   source in lockstep — a mismatch yields `401 Invalid HMAC Signature`.
5. Rotating `ENCRYPTION_KEY` (Fernet) re-keys stored secrets and is a planned
   maintenance operation — never rotate it without a re-encryption plan, or
   stored bearer tokens/HMAC secrets become undecryptable.

HMAC senders must sign `<timestamp>.<nonce>.<raw body>` and provide
`X-HookWise-Signature`, `X-HookWise-Timestamp`, and `X-HookWise-Nonce`. Nonces
cannot be reused and timestamps are accepted only within a five-minute window.

---

## Worker / beat not processing

1. `GET /health/services` — check the `celery` field (`up` / `warning` / `down`)
   and `celery_active`.
2. If `down`: confirm the worker container is running and can reach Redis
   (`redis` field). Restart the worker.
3. If webhooks are `queued` but never `processed`: the worker is not consuming —
   check broker connectivity and worker logs.
4. Long-running tasks are bounded by `CELERY_TASK_SOFT_TIME_LIMIT` (default 120s)
   and `CELERY_TASK_TIME_LIMIT` (default 300s); a task exceeding these is
   killed and retried/dead-lettered rather than hanging a worker.

---

## Roles, sign-in & lockout recovery

Roles are enforced by default (`RBAC_ENFORCE=on`). Denied requests return 403
and are written to the audit log as `perm_denied`.

**"Someone can't do X anymore" triage**
1. Identity page → expand the user → check the assigned role; the permission
   matrix (Permissions tab) shows exactly what each role grants.
2. `/audit` filtered for `perm_denied` names the missing permission per event.
3. If a legitimate workflow is blocked and needs time to sort out, set
   `RBAC_ENFORCE=log` (env) and restart — everything works again while every
   would-be denial is still logged. Revert to `on` afterwards.

**Locked out (no admin can sign in or manage users)**
The app refuses to deactivate/delete the last `user:manage` holder, so this
state normally cannot be reached through the UI. If it happens anyway
(lost password, disabled account):
1. `BOOTSTRAP_ADMIN=true` + `GUI_PASSWORD=<new>` on the web container
   recreates/re-syncs the local `admin` account (role `admin`) at startup.
2. Alternatively `RBAC_ENFORCE=log` grants temporary access to repair role
   assignments through the Identity page.

**MFA reset**: Identity page → expand the user → *Reset MFA* (visible only
when enrolled). The user signs in with password only until re-enrolling under
Settings → Two-Factor Auth.

**Entra ID outage**: local accounts are unaffected — keep at least one local
break-glass admin. Entra accounts cannot fall back to passwords (they have
none). The Entra ID tab on the Identity page shows the connection state.

**Headless/API clients** authenticate via Basic Auth against
`GUI_USERNAME`/`GUI_PASSWORD` *and* need an active HookWise account with that
username; the account's roles decide what the client may call.

---

## Configuration & limits (env)

| Variable | Default | Purpose |
|----------|---------|---------|
| `SECRET_KEY` | — (required in prod) | Flask session signing |
| `GUI_PASSWORD` | — (required) | admin login password (also basic-auth secret for headless clients) |
| `RBAC_ENFORCE` | `on` | role enforcement: `on` / `log` (check + log only) / `off` |
| `ENCRYPTION_KEY` | — (required) | Fernet key for stored secrets |
| `SESSION_COOKIE_SECURE` | `true` | send session cookie only over HTTPS |
| `MAX_CONTENT_LENGTH_KB` | `1024` | reject inbound bodies above this size (413) |
| `CELERY_TASK_SOFT_TIME_LIMIT` | `120` | soft task limit (seconds) |
| `CELERY_TASK_TIME_LIMIT` | `300` | hard task limit (seconds) |
| `VIABILITY_TTL` | `300` | ticket-dedup viability window (seconds) |
| `LOG_RETENTION_DAYS` | `30` | history retention for the daily cleanup task |

---

## Auditing

Security-relevant events are written to `AuditLog` and viewable at `/audit`:
logins (success and **failed** attempts), 2FA enable/disable, logout,
maintenance toggles, and configuration changes. Identity & access adds:
`perm_denied`, `user_create` / `user_update` / `user_delete`, `role_grant`,
`role_create` / `role_update` / `role_delete`, `user_password_reset`,
`user_mfa_reset`, `entra_login` / `entra_login_denied`,
`entra_auto_provisioned`, `entra_binding_reset` and `entra_settings_update`.
Review it after any suspected unauthorized access or unexpected config change.
