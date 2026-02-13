<p align="center">
  <img src="docs/logo.png" alt="HookWise Logo" width="400">
</p>

# HookWise

**Webhook Router & ConnectWise Bridge**

HookWise is a powerful, general-purpose webhook router designed to bridge various monitoring sources (Uptime Kuma, Zabbix, Grafana, etc.) to **ConnectWise Manage** tickets. It features a modern Web GUI, intelligent duplicate detection, and locally-hosted AI for automated root cause analysis.

---

## 🚀 Key Features

### 🛠️ Advanced Routing & Mapping
- **Dynamic Endpoints:** Generate unique URLs and Bearer tokens for every monitoring source.
- **JSONPath Mapping:** Flexible extraction of any field from incoming payloads to map to ticket summaries, descriptions, or custom fields.
- **Regex Routing Rules:** Apply complex logic to override ticket fields (Board, Status, Priority) based on payload content.
- **Duplicate Detection:** Smart caching and PSA lookups prevent duplicate tickets for the same event.
- **Automatic Company Resolution:** Extract ConnectWise Company IDs directly from alert titles using the `#CW` prefix or mapped JSON fields.

### 🧠 Intelligent Automation
- **AI Root Cause Analysis:** Integrates with local LLMs (via Ollama) to automatically analyze alerts and provide troubleshooting suggestions as internal ticket notes.
- **Auto-Resolution:** Automatically closes open tickets when a recovery (UP) webhook is received.
- **Maintenance Windows:** Schedule quiet periods per endpoint to silence alerts during planned maintenance.

### 📋 Observability & Admin
- **Web GUI:** Modern interface with live activity feed (Socket.io), skeleton loading, and keyboard shortcuts (`/` to search).
- **Audit Logging:** Comprehensive tracking of all configuration changes and admin actions.
- **Webhook Replay:** Effortlessly re-trigger any received webhook for debugging or recovery.
- **Debug Processor:** Built-in tool to test JSONPath and Routing Rules against sample payloads with step-by-step resolution logs.
- **Metrics & Health:** Native Prometheus `/metrics` endpoint and detailed `/health/services` reporting.

### 🔒 Enterprise Security
- **2FA Support:** Secure admin access with TOTP (Google Authenticator, etc.).
- **IP Whitelisting:** Restrict GUI access or individual webhook endpoints to trusted IP ranges.
- **HMAC Verification:** Validate incoming webhooks using secret-key signatures.
- **Field Encryption:** Sensitive configuration tokens and keys are encrypted at rest.
- **Local Assets:** All JS/CSS dependencies are hosted locally for privacy and air-gapped support.

---

## 🏗️ Technical Architecture

HookWise is built for reliability and scale:

- **Frontend:** Flask with a responsive, premium UI.
- **Backend:** Python + Gevent for high-concurrency webhook handling.
- **Task Queue:** Celery + Redis for reliable background processing and retries.
- **Database:** PostgreSQL for robust configuration and log storage.
- **AI Engine:** Ollama integration for local, private LLM execution.
- **Monitoring:** Prometheus integration for real-time performance tracking.

---

## ⚙️ Configuration

The application is configured via environment variables.

| Category | Variable | Description | Default |
|----------|----------|-------------|---------|
| **ConnectWise** | `CW_URL` | ConnectWise API Base URL | `https://api-na.../3.0` |
| | `CW_COMPANY` | Your ConnectWise Company ID | **Required** |
| | `CW_PUBLIC_KEY` | API Public Key | **Required** |
| | `CW_PRIVATE_KEY` | API Private Key | **Required** |
| | `CW_CLIENT_ID` | API Client ID | **Required** |
| **Database** | `DATABASE_URL` | PostgreSQL Connection String | **Required** |
| **Redis** | `REDIS_PASSWORD`| Password for Redis and Celery | **Required** |
| | `REDIS_HOST` | Redis hostname | `redis` |
| **Security** | `SECRET_KEY` | Flask session secret key | auto-generated |
| | `ENCRYPTION_KEY` | 32-byte Base64 key for encryption | **Recommended** |
| | `GUI_PASSWORD` | Admin password | `admin` |
| **Integrations**| `OLLAMA_HOST` | URL for local AI service | `http://hookwise-llm:11434`|
| | `LOG_RETENTION_DAYS`| Days to keep webhook history | `30` |

---

## 📦 Deployment

### Docker Compose (Recommended)

1. **Prepare Environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

2. **Launch Services:**
   ```bash
   docker-compose up -d
   ```

3. **Initialize Database:**
   ```bash
   docker-compose exec hookwise-proxy flask db upgrade
   ```

4. **Enable AI Analysis (Optional):**
   ```bash
   docker exec -it hookwise-llm ollama pull phi3
   ```

Access the Web GUI at `http://localhost:5000`. Default: `admin` / `admin`.

---

## 📖 Usage Guide

1. **Create Endpoint:** Go to "Endpoints" -> "New Endpoint". Define your Service Board and mapping.
2. **Configure JSONPath:** Use the "Debug Tool" to paste a sample payload and verify your mappings.
3. **Set Up Source:** Copy the unique URL and Bearer token into your monitoring software (e.g., Uptime Kuma).
4. **Advanced Routing:** Add Regex rules if you need to route specific alerts (e.g., "Critical") to different boards or priorities.
5. **Monitor:** Watch the "Live Activity" feed on the dashboard for real-time processing updates.

---

## 🛠️ Development

### Local Setup
```bash
pip install -r requirements.txt
flask db upgrade
python app.py
```

### Testing & Quality
```bash
# Run Unit Tests
pytest

# Linting
ruff check .
```

---

## 🛡️ Security
Security is a core pillar of HookWise. We recommend:
- Rotating the `ENCRYPTION_KEY` annually.
- Enabling TOTP for all admin accounts.
- Restricting `GUI_TRUSTED_IPS` to your management subnet.

---

## 📄 License
MIT License - Copyright (c) 2024 HookWise Team.

