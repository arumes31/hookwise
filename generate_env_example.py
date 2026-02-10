
def generate_env_example():
    example_content = """# ConnectWise API Configuration
CW_URL=https://api-na.myconnectwise.net/v4_6_release/apis/3.0
CW_COMPANY=your_company_id
CW_PUBLIC_KEY=your_public_key
CW_PRIVATE_KEY=your_private_key
CW_CLIENT_ID=your_client_id

# ConnectWise Ticket Defaults
CW_SERVICE_BOARD=Service Board
CW_STATUS_NEW=New
CW_STATUS_CLOSED=Closed
CW_DEFAULT_COMPANY_ID=ConnectWise_Company_ID_For_Tickets
CW_TICKET_PREFIX=Alert:

# Redis Configuration
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=your_secure_redis_password

# Database Configuration
# DATABASE_URL=sqlite:///hookwise.db
DATABASE_URL=postgresql://hookwise:hookwise_pass@localhost:5432/hookwise

# Flask Configuration
SECRET_KEY=your_very_secret_key
PORT=5000
USE_PROXY=false
PROXY_FIX_COUNT=1

# Web GUI Auth
GUI_USERNAME=admin
GUI_PASSWORD=password

# Observability
DEBUG_MODE=false
LOG_RETENTION_DAYS=30
ENCRYPTION_KEY=your_32_byte_base64_encryption_key

# Celery
CELERY_BROKER_URL=redis://:your_secure_redis_password@localhost:6379/0
"""
    with open(".env.example", "w") as f:
        f.write(example_content)
    print(".env.example generated successfully.")

if __name__ == "__main__":
    generate_env_example()
