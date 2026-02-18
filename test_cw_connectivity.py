import base64
import logging
import os

import requests

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_connection() -> None:
    # Load credentials from environment
    base_url = os.getenv("CW_URL", "https://api-na.myconnectwise.net/v4_6_release/apis/3.0")
    company = os.getenv("CW_COMPANY")
    public_key = os.getenv("CW_PUBLIC_KEY")
    private_key = os.getenv("CW_PRIVATE_KEY")
    client_id = os.getenv("CW_CLIENT_ID")

    print(f"Testing connectivity to: {base_url}")
    print(f"Company: {company}")
    print(f"Public Key: {public_key[:4]}..." if public_key else "Public Key: Not Set")
    print(f"Private Key: {'*' * 8}" if private_key else "Private Key: Not Set")
    print(f"Client ID: {client_id if client_id else 'Not Set'}")

    if not all([company, public_key, private_key]):
        print("ERROR: Missing required credentials (CW_COMPANY, CW_PUBLIC_KEY, CW_PRIVATE_KEY)")
        return

    # Construct Auth Header
    auth_string = f"{company}+{public_key}:{private_key}"
    auth_header = f"Basic {base64.b64encode(auth_string.encode()).decode()}"
    headers = {"Authorization": auth_header, "Content-Type": "application/json", "Accept": "application/json"}

    if client_id:
        headers["clientId"] = client_id

    # Test Endpoints
    endpoints = [
        ("/system/info", "System Info"),
        ("/service/boards", "Service Boards"),
        ("/service/priorities", "Priorities"),
    ]

    for endpoint, name in endpoints:
        url = f"{base_url}{endpoint}"
        print(f"\n--- Testing {name} ({url}) ---")
        try:
            response = requests.get(url, headers=headers, timeout=10)
            print(f"Status Code: {response.status_code}")

            if response.status_code == 200:
                print("Success!")
                try:
                    data = response.json()
                    if isinstance(data, list):
                        print(f"Items returned: {len(data)}")
                        if len(data) > 0:
                            print(f"First item: {data[0].get('name', 'No Name')}")
                    else:
                        print("Response OK (JSON Object)")
                except Exception as e:
                    print(f"Could not parse JSON: {e}")
            elif response.status_code == 403:
                print("FORBIDDEN (403). Possible causes:")
                print("1. API Member does not have permission for this endpoint.")
                print("2. IP Address is not whitelisted in ConnectWise.")
                print("3. Application Client ID is invalid or missing.")
            else:
                print(f"Failed. Response: {response.text[:200]}")

        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    test_connection()
