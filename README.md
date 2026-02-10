<p align="center">
  <img src="docs/logo.png" alt="HookWise Logo" width="400">
</p>

# HookWise

A general-purpose webhook router that bridges various webhooks to **ConnectWise Manage** tickets with a user-friendly Web GUI.

## Features

- **Web GUI:** Easily create and manage webhook endpoints.
- **Dynamic Endpoints:** Generate unique URLs for different monitoring sources.
- **General Webhook Router:** Receives alerts from any source and routes them to ConnectWise.
- **Customizable Configuration:** Configure Service Board, Status, Type, Subtype, and Priority per endpoint.
- **Auto-Ticketing:** Creates tickets in ConnectWise based on incoming webhook data.
- **Smart Parsing:** Extracts Company ID from titles using the `#CW` prefix (e.g., `My Server #CW123`).
- **Bearer Token Auth:** Every generated endpoint is secured with a unique Bearer token (stored encrypted).
- **Security First:** Includes IP Whitelisting, Basic Auth for the GUI, and field encryption.
- **Reliability:** Built-in retry mechanism with exponential backoff for PSA calls.
- **Observability:** Real-time log feed, detailed Prometheus metrics, and service health dashboard.
- **Enterprise Ready:** Uses PostgreSQL for production-grade storage and includes database migrations.

## Configuration

The application is configured via environment variables. Use `python generate_env_example.py` to create a template.

| Variable | Description | Required | Default |
|----------|-------------|:--------:|---------|
| `CW_URL` | ConnectWise API Base URL | No | `https://api-na.myconnectwise.net/v4_6_release/apis/3.0` |
| `CW_COMPANY` | Your ConnectWise Company ID | **Yes** | - |
| `CW_PUBLIC_KEY` | API Public Key | **Yes** | - |
| `CW_PRIVATE_KEY` | API Private Key | **Yes** | - |
| `CW_CLIENT_ID` | API Client ID | **Yes** | - |
| `DATABASE_URL` | DB URL (Postgres recommended) | No | `postgresql://hookwise:hookwise_pass@postgres:5432/hookwise` |
| `GUI_USERNAME` | Basic Auth Username for GUI | No | - |
| `GUI_PASSWORD` | Basic Auth Password for GUI | No | - |
| `ENCRYPTION_KEY` | 32-byte Base64 key for data | No | auto-generated |
| `REDIS_PASSWORD` | Password for Redis security | No | - |
| `CELERY_BROKER_URL` | Redis connection string | No | `redis://redis:6379/0` |

## Deployment

### Docker Compose (Recommended)

1. Generate environment file: `python generate_env_example.py`
2. Update `.env.example` to `.env` and fill in credentials.
3. Start services: `docker-compose up -d`
4. Apply migrations: `docker-compose exec kumawise-proxy flask db upgrade`

Access the Web GUI at `http://localhost:5000`.

## Usage

1. **Access the Web GUI:** Open HookWise in your browser.
2. **Create Endpoint:** Click "New Endpoint", fill in the details (Board, Status, etc.), and save.
3. **Get Endpoint URL:** Copy the auto-generated URL and Bearer Token.
4. **Configure Source:** Set your source (e.g., Uptime Kuma) to send webhooks to that URL.
5. **Authenticate:** Ensure your source sends the `Authorization: Bearer <your-token>` header.

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
