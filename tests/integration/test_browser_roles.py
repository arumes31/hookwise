"""Each role can use the pages it is offered without hidden errors.

Server-side tests cannot see requests made by page scripts or controls hidden
by CSS: the Entra dashboard 403 came from a background request, and the
maintenance switch had been invisible for weeks. This drives a real browser.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from .stack import APP_PRELUDE, BASE_URL, requires_stack, run_python

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = requires_stack

PASSWORD = "Integration-Only-Login-2026"
ROLES = ("viewer", "operator", "admin")
NAV_LINKS = 'nav[aria-label="Primary navigation"] a.nav-link[href^="/"]'


@pytest.fixture(scope="module")
def accounts() -> dict[str, str]:
    """Create local viewer, operator and admin accounts with a known password."""
    run_python(
        "hookwise-worker",
        APP_PRELUDE
        + f"""
from werkzeug.security import generate_password_hash
from hookwise.models import User
with app.app_context():
    for role in {list(ROLES)!r}:
        user = User.query.filter_by(username=f"it-{{role}}").first() or User(username=f"it-{{role}}")
        user.password_hash = generate_password_hash({PASSWORD!r})
        user.role = role
        user.auth_source = "local"
        user.is_active = True
        user.is_2fa_enabled = False
        db.session.add(user)
    db.session.commit()
print("RESULT {{}}")
""",
    )
    return {role: f"it-{role}" for role in ROLES}


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    """Launch one Chromium instance for the module."""
    with sync_api.sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


class Session:
    """A signed-in browser page that records failed requests and script errors."""

    def __init__(self, browser: Any, username: str) -> None:
        """Open a new browser context and sign in through the login form."""
        self.context = browser.new_context()
        self.page = self.context.new_page()
        self.problems: list[str] = []
        self.page.on("response", self._on_response)
        self.page.on("pageerror", lambda error: self.problems.append(f"script error: {error}"))
        self.page.goto(f"{BASE_URL}/login")
        self.page.fill("#username", username)
        self.page.fill("#password", PASSWORD)
        self.page.click("#submit-btn")
        self.page.wait_for_url(f"{BASE_URL}/")

    def _on_response(self, response: Any) -> None:
        """Record same-origin responses with an error status."""
        if response.url.startswith(BASE_URL) and response.status >= 400:
            self.problems.append(f"{response.status} {response.request.method} {response.url}")

    def open(self, path: str) -> None:
        """Open a console page and wait for its first background requests."""
        self.page.goto(f"{BASE_URL}{path}")
        self.page.wait_for_load_state("load")
        # Give page scripts time to issue their first background requests.
        self.page.wait_for_timeout(1500)

    def close(self) -> None:
        """Close the browser context."""
        self.context.close()


@pytest.fixture
def signed_in(browser: Any, accounts: dict[str, str]) -> Iterator[Any]:
    """Factory for signed-in sessions that are closed after the test."""
    sessions: list[Session] = []

    def sign_in(role: str) -> Session:
        """Sign in as the given role and keep the session for cleanup."""
        session = Session(browser, accounts[role])
        sessions.append(session)
        return session

    yield sign_in
    for session in sessions:
        session.close()


@pytest.mark.parametrize("role", ROLES)
def test_every_offered_page_loads_without_errors(signed_in: Any, role: str) -> None:
    """Every navigation page loads for the role without 4xx/5xx responses or script errors."""
    session = signed_in(role)
    links = session.page.eval_on_selector_all(
        NAV_LINKS, "elements => [...new Set(elements.map(element => element.getAttribute('href')))]"
    )
    links = [link for link in links if not link.startswith("/logout")]
    assert "/" in links and "/webhooks" in links, links

    for link in links:
        session.open(link)
    assert not session.problems, f"{role}: " + "\n".join(session.problems)


def test_admin_can_reach_the_maintenance_switch(signed_in: Any) -> None:
    """Admins find the maintenance switch from System settings and can use it."""
    session = signed_in("admin")
    session.open("/settings")
    assert session.page.get_by_role("link", name="Manage maintenance mode").is_visible()
    session.open("/admin/maintenance")
    assert session.page.locator("#maintenance-form button[type=submit]").is_visible()
    assert not session.problems, "\n".join(session.problems)


def test_viewer_sees_maintenance_state_read_only(signed_in: Any) -> None:
    """Viewers see the maintenance state without a switch and without errors."""
    session = signed_in("viewer")
    session.open("/admin/maintenance")
    assert session.page.get_by_text("Read-only").is_visible()
    assert session.page.locator("#maintenance-form").count() == 0
    assert not session.problems, "\n".join(session.problems)
