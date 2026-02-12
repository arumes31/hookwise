<p align="center">
  <img src="docs/logo.png" alt="HookWise Logo" width="400">
</p>

# HookWise

A general-purpose webhook router that bridges various webhooks to **ConnectWise Manage** tickets with a user-friendly Web GUI.

## Features

- **Web GUI:** Easily create and manage webhook endpoints with advanced search, filters, and drag-and-drop reordering.
- **Dynamic Endpoints:** Generate unique URLs for different monitoring sources.
- **Advanced UX:** Sparklines, live activity feed, skeleton loading, and keyboard shortcuts (press `/` to search).
- **Customizable Configuration:** Configure Service Board, Status, Type, Subtype, and Priority per endpoint with JSONPath mapping support.
- **Auto-Ticketing:** Creates tickets in ConnectWise based on incoming webhook data with duplicate detection.
- **Smart Parsing:** Extracts Company ID from titles using the `#CW` prefix (e.g., `My Server #CW123`).
- **Security First:** 2FA with TOTP/QR, IP Whitelisting, HMAC signature verification, and field encryption.
- **Reliability:** Built-in retry mechanism with exponential backoff for PSA calls.
- **Observability:** Service health dashboard, detailed Prometheus metrics, and audit logging.
- **Enterprise Ready:** PostgreSQL storage, Alembic migrations, and maintenance mode support.

## Configuration

The application is configured via environment variables. An example file is provided in `.env.example`.

| Category | Variable | Description | Default |
|----------|----------|-------------|---------|
| **ConnectWise** | `CW_URL` | ConnectWise API Base URL | `https://api-na.../3.0` |
| | `CW_COMPANY` | Your ConnectWise Company ID | **Required** |
| | `CW_PUBLIC_KEY` | API Public Key | **Required** |
| | `CW_PRIVATE_KEY` | API Private Key | **Required** |
| | `CW_CLIENT_ID` | API Client ID | **Required** |
| | `CW_TICKET_PREFIX` | Default prefix for ticket summaries | `Alert:` |
| **Database** | `DATABASE_URL` | DB URL (Postgres recommended) | `postgresql://...` |
| | `POSTGRES_PASSWORD`| Password for the PostgreSQL container | `hookwise_pass` |
| **Redis/Celery** | `REDIS_PASSWORD` | Password for Redis and Celery broker | **Required** |
| | `REDIS_HOST` | Redis hostname | `redis` |
| **Security** | `SECRET_KEY` | Flask session secret key | auto-generated |
| | `ENCRYPTION_KEY` | 32-byte Base64 key for encrypting tokens | **Recommended** |
| | `GUI_TRUSTED_IPS` | Comma-separated list of allowed IPs for GUI | - |
| **App** | `DEBUG_MODE` | Enable debug logging and visual aids | `false` |
| | `FORCE_HTTPS` | Force redirect to HTTPS | `false` |
| | `LOG_RETENTION_DAYS`| Days to keep webhook history | `30` |

## Deployment

### Docker Compose (Recommended)

1. Copy the example environment file: `cp .env.example .env`
2. Update `.env` with your ConnectWise credentials and secure passwords.
3. Start services: `docker-compose up -d`
4. Apply migrations: `docker-compose exec hookwise-proxy flask db upgrade`

Access the Web GUI at `http://localhost:5000`. Default login is `admin` / `admin` (can be changed via `GUI_PASSWORD`).

## Usage

1. **Access the Web GUI:** Open HookWise and log in.
2. **Create Endpoint:** Click "New Endpoint", fill in the details, and use the "Test Path" tool to verify your JSON mapping.
3. **Get Endpoint URL:** Copy the generated URL and Bearer Token from the dashboard.
4. **Configure Source:** Set your alert source (e.g., Uptime Kuma) to send webhooks to that URL.
5. **Security:** Add the `Authorization: Bearer <your-token>` header in your source settings. For added security, configure an **HMAC Secret** and check for the `X-HookWise-Signature` header.

### Monitor Naming Convention

Include `#CW<CompanyIdentifier>` in your monitor or alert title to automatically route the ticket to a specific company in ConnectWise.

**Example:** `Server Down #CWClientA`

## Security

- **Local Assets:** All JS and CSS files are hosted locally to ensure privacy and offline capability.
- **Bearer Tokens:** Each endpoint is protected by a unique, secure token.

## Development

### Running Tests
```bash
pytest
```

## License

This project is licensed under the MIT License.
