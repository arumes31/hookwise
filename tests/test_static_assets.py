import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import flask
import pytest

from hookwise import _canonical_static_asset_name, create_app

ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "static"
TEMPLATE_ROOT = ROOT / "templates"
ONE_YEAR_SECONDS = 31_536_000


@pytest.fixture
def app():
    return create_app(
        {
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        }
    )


@pytest.fixture
def client(app):
    return app.test_client()


def _content_digest(filename: str) -> str:
    return hashlib.sha256((STATIC_ROOT / filename).read_bytes()).hexdigest()[:12]


def test_static_asset_helper_uses_file_content_digest(app):
    filename = "js/dashboard.js"

    with app.test_request_context():
        asset_url = flask.render_template_string("{{ static_asset('js/dashboard.js') }}")

    parsed = urlsplit(asset_url)
    assert parsed.path == f"/static/{filename}"
    assert parse_qs(parsed.query) == {"v": [_content_digest(filename)]}


def test_templates_use_static_asset_helper_for_local_assets():
    legacy_url_for = re.compile(r"url_for\(\s*['\"]static['\"]")
    helper_call = re.compile(r"static_asset\(\s*['\"]([^'\"]+)['\"]\s*\)")
    raw_static_url = re.compile(r"(?:src|href)\s*=\s*['\"]/static/")
    referenced_assets: list[tuple[Path, str]] = []

    for template in TEMPLATE_ROOT.rglob("*.html"):
        source = template.read_text(encoding="utf-8")
        assert not legacy_url_for.search(source), f"{template} still manually versions a static URL"
        assert not raw_static_url.search(source), f"{template} bypasses the static asset helper"
        referenced_assets.extend((template, filename) for filename in helper_call.findall(source))

    assert referenced_assets, "templates do not use the static_asset helper"
    for template, filename in referenced_assets:
        assert (STATIC_ROOT / filename).is_file(), f"{template} references missing static asset {filename}"


def test_runtime_javascript_does_not_bypass_versioned_asset_urls():
    for script in (STATIC_ROOT / "js").glob("*.js"):
        assert "/static/" not in script.read_text(encoding="utf-8"), f"{script} contains an unversioned asset URL"


def test_high_impact_ui_actions_keep_explicit_confirmations():
    """Guard destructive and externally visible actions against one-click execution."""
    sources = {
        "account": (TEMPLATE_ROOT / "account_settings.html").read_text(encoding="utf-8"),
        "history": (TEMPLATE_ROOT / "history.html").read_text(encoding="utf-8"),
        "identity": (TEMPLATE_ROOT / "identity.html").read_text(encoding="utf-8"),
        "index": (TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8"),
        "webhooks": (TEMPLATE_ROOT / "webhooks.html").read_text(encoding="utf-8"),
        "dashboard": (STATIC_ROOT / "js" / "dashboard-page.js").read_text(encoding="utf-8"),
        "history_operations": (STATIC_ROOT / "js" / "history-operations.js").read_text(encoding="utf-8"),
        "ux": (STATIC_ROOT / "js" / "ux.js").read_text(encoding="utf-8"),
    }

    assert "confirmSessionRevoke(this, false)" in sources["account"]
    assert "confirmSessionRevoke(this, true)" in sources["account"]
    assert 'class="hw-session-footer hw-session-revoke-form"' in sources["account"]
    assert "Replay selected webhooks" in sources["history"]
    assert "!SERVER_DATA.debugMode" not in sources["history"]
    assert "Retry this failed request now?" in sources["history_operations"]
    assert "Replay edited request" in sources["history_operations"]
    assert "Set a new password for " in sources["identity"]
    assert "Enable automatic Entra provisioning?" in sources["identity"]
    assert "toggleMaintenance(this)" in sources["index"]
    assert "Enable maintenance mode?" in sources["dashboard"]
    assert "Send a test webhook through" in sources["dashboard"]
    assert "archiveEndpoint(this.dataset.endpointId, this.dataset.endpointName, this)" in sources["webhooks"]
    assert (
        '<button type="button" class="hw-more-row hw-endpoint-action-row" data-braucht="endpoint:archive"'
        in sources["webhooks"]
    )
    assert "toggleEndpoint(this.dataset.endpointId, this.dataset.endpointEnabled === 'true'" in sources["webhooks"]
    assert "Future webhook deliveries use these routing values immediately." in sources["webhooks"]
    assert "window.archiveEndpoint = async" in sources["ux"]
    assert "Pause ${checked.length} selected endpoints?" in sources["ux"]
    assert "hwConfirmPending" in sources["ux"]
    assert "bootstrap.Modal.getOrCreateInstance" in sources["ux"]


def test_error_page_retries_do_not_repeat_unsafe_requests(app):
    """Offer reload only when it cannot repeat a state-changing request."""
    with app.test_request_context("/settings", method="GET"):
        bad_request_get = flask.render_template("400.html")
        server_error_get = flask.render_template("500.html")
    assert "window.location.reload()" in bad_request_get
    assert "window.location.reload()" in server_error_get

    with app.test_request_context("/settings", method="POST"):
        bad_request_post = flask.render_template("400.html")
        server_error_post = flask.render_template("500.html")
    assert "window.location.reload()" not in bad_request_post
    assert 'href="/settings"' in bad_request_post
    assert "Open a fresh form" in bad_request_post
    assert "window.location.reload()" not in server_error_post
    assert "Check operation status" in server_error_post
    assert "may already have completed" in server_error_post


def test_stylesheet_local_assets_use_their_content_digest():
    local_url = re.compile(r"url\(\s*['\"]?(?!data:)([^'\")]+)")

    for stylesheet in (STATIC_ROOT / "css").glob("*.css"):
        source = stylesheet.read_text(encoding="utf-8")
        for reference in local_url.findall(source):
            parsed = urlsplit(reference)
            asset = (stylesheet.parent / parsed.path).resolve()
            filename = asset.relative_to(STATIC_ROOT).as_posix()
            assert asset.is_file(), f"{stylesheet} references missing static asset {reference}"
            assert parse_qs(parsed.query) == {"v": [_content_digest(filename)]}, (
                f"{stylesheet} does not content-version {reference}"
            )


def test_correctly_versioned_static_asset_is_cached_immutably(client):
    filename = "js/dashboard.js"

    response = client.get(f"/static/{filename}?v={_content_digest(filename)}")

    assert response.status_code == 200
    assert response.cache_control.public is True
    assert response.cache_control.max_age == ONE_YEAR_SECONDS
    assert response.cache_control.immutable is True
    assert response.cache_control.no_cache is not True


def test_static_cache_policy_does_not_resolve_request_controlled_paths(app, monkeypatch):
    filename = "js/dashboard.js"
    asset_url = f"/static/{filename}?v={_content_digest(filename)}"

    def reject_filesystem_resolution(*_args, **_kwargs):
        raise AssertionError("request-controlled static filenames must not reach Path.resolve")

    monkeypatch.setattr(Path, "resolve", reject_filesystem_resolution)

    response = app.test_client().get(asset_url)

    assert response.status_code == 200
    assert response.cache_control.immutable is True


@pytest.mark.parametrize(
    "filename",
    [
        "",
        ".",
        "js/..",
        "../js/dashboard.js",
        "js/../../dashboard.js",
        "/js/dashboard.js",
        "js\\dashboard.js",
        "js/\0dashboard.js",
    ],
)
def test_static_asset_name_rejects_unsafe_or_empty_paths(filename):
    with pytest.raises(ValueError, match="Unknown static asset"):
        _canonical_static_asset_name(filename)


@pytest.mark.parametrize(
    "query",
    ["", "?v=not-the-current-content-hash", "?v=%C3%A9", "?v=%F0%9F%98%80"],
)
def test_unversioned_or_incorrectly_versioned_static_asset_must_revalidate(client, query):
    response = client.get(f"/static/js/dashboard.js{query}")

    assert response.status_code == 200
    assert response.cache_control.no_cache is True
    assert response.cache_control.immutable is False
    assert response.cache_control.public is not True
    assert response.cache_control.max_age in (None, 0)


def test_conditional_response_keeps_immutable_policy_for_correct_version(client):
    filename = "js/dashboard.js"
    asset_url = f"/static/{filename}?v={_content_digest(filename)}"
    initial = client.get(asset_url)

    assert initial.status_code == 200
    etag = initial.headers.get("ETag")
    assert etag

    response = client.get(asset_url, headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert response.cache_control.public is True
    assert response.cache_control.max_age == ONE_YEAR_SECONDS
    assert response.cache_control.immutable is True


def test_static_path_aliases_cannot_expand_the_version_manifest(app, client):
    manifest = app.extensions.get("static_asset_versions")
    assert manifest is not None
    original_keys = set(manifest)
    digest = _content_digest("js/dashboard.js")

    for alias in ("js/dashboard.js", "js/./dashboard.js", "js/temporary/../dashboard.js"):
        response = client.get(f"/static/{alias}?v={digest}")
        assert response.status_code == 200
        assert response.cache_control.immutable is True

    assert set(manifest) == original_keys
    assert "js/./dashboard.js" not in manifest
    assert "js/temporary/../dashboard.js" not in manifest
