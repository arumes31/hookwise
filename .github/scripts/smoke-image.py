"""Exercise a running candidate over HTTP, with real CSRF and session cookies."""

import os
import time
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, build_opener


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.csrf_token = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "csrf_token":
            self.csrf_token = attributes.get("value")


def main():
    base_url = os.environ.get("STAGING_URL", "http://127.0.0.1:5000").rstrip("/")
    client = build_opener(HTTPCookieProcessor(CookieJar()))
    for attempt in range(60):
        try:
            for path in ("/health", "/readyz"):
                with client.open(base_url + path, timeout=3) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Candidate probe failed: {path}")
            break
        except OSError:
            pass
        if attempt == 59:
            raise RuntimeError("Candidate did not become healthy")
        time.sleep(2)

    with client.open(base_url + "/login", timeout=10) as response:
        login_html = response.read().decode()
    form = LoginForm()
    form.feed(login_html)
    if not form.csrf_token or 'id="login-form"' not in login_html:
        raise RuntimeError("Login form or CSRF token is missing")

    credentials = urlencode(
        {
            "csrf_token": form.csrf_token,
            "username": os.environ["STAGING_USERNAME"],
            "password": os.environ["STAGING_PASSWORD"],
        }
    ).encode()
    with client.open(base_url + "/login", data=credentials, timeout=20) as response:
        dashboard = response.read().decode()
        if urlsplit(response.url).path != "/" or 'id="operations-dashboard"' not in dashboard:
            raise RuntimeError("Candidate did not authenticate into the dashboard")

    # A second request verifies that authentication persisted in the cookie jar.
    with client.open(base_url + "/api/stats", timeout=10) as response:
        if response.headers.get_content_type() != "application/json":
            raise RuntimeError("Authenticated stats endpoint did not return JSON")
    print("PASS: health, login form, CSRF, administrator sign-in, dashboard and authenticated API")


if __name__ == "__main__":
    main()
