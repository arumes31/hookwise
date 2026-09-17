import re
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import GlobalMapping


@pytest.fixture
def app():
    """Create an isolated in-memory HookWise application."""
    return create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})


@pytest.fixture
def client(app):
    """Yield a client with a fresh TenantMap schema."""
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def disable_mapping_cache_revision():
    """Keep route tests independent from a running Redis service."""
    with patch("hookwise.tenantmap.bump_mapping_cache_revision"):
        yield


def _authenticate(client):
    """Authenticate the test client as a legacy administrator."""
    with client.session_transaction() as session:
        session["user_id"] = "tenantmap-user"
        session["username"] = "admin"
        session["role"] = "admin"


def test_tenantmap_forms_use_interactive_bootstrap_modal_structure(client, app):
    """Keep mapping forms inside Bootstrap's interactive modal content."""
    _authenticate(client)
    with app.app_context():
        db.session.add(
            GlobalMapping(
                tenant_value="searchable.example",
                company_id="COMPANY-SEARCH",
                description="Search fixture",
            )
        )
        db.session.commit()

    response = client.get("/tenantmap")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert html.count('hx-boost="false"') >= 2
    for modal_id in ("addMappingModal", "editMappingModal"):
        assert re.search(
            rf'id="{modal_id}".*?<div class="modal-dialog modal-dialog-centered">\s*'
            r'<div class="modal-content[^\"]*glass-card',
            html,
            re.DOTALL,
        )
    assert 'label for="add_tenant_values"' in html
    assert 'label for="edit_tenant_values"' in html
    assert 'id="tenantmap-search"' in html
    assert 'id="tenantmap-field-filter"' in html
    assert 'id="tenantmap-result-count"' in html
    assert 'id="tenantmap-no-results" hidden' in html
    assert 'class="hw-tenantmap-row"' in html


def test_content_modals_are_mounted_above_the_body_level_backdrop():
    """Mount content dialogs at body level above Bootstrap's backdrop."""
    with open("static/js/ux.js", encoding="utf-8") as ux_script:
        source = ux_script.read()

    assert "mountContentModals(container);" in source
    assert "container.querySelectorAll('#main-content .modal')" in source
    assert "modal.ownerDocument.body.appendChild(modal)" in source


def test_tenantmap_create_and_edit_persist(client, app):
    """Persist normalized values through both mapping write routes."""
    _authenticate(client)

    create_response = client.post(
        "/tenantmap/add",
        data={
            "tenant_value": " tenant.example ",
            "company_id": " COMPANY-1 ",
            "description": " Initial mapping ",
        },
    )

    assert create_response.status_code == 302
    with app.app_context():
        mapping = GlobalMapping.query.one()
        mapping_id = mapping.id
        assert mapping.tenant_value == "tenant.example"
        assert mapping.company_id == "COMPANY-1"
        assert mapping.description == "Initial mapping"

    edit_response = client.post(
        f"/tenantmap/edit/{mapping_id}",
        data={
            "tenant_value": "renamed.example",
            "company_id": "COMPANY-2",
            "description": "Updated mapping",
        },
    )

    assert edit_response.status_code == 302
    with app.app_context():
        mapping = db.session.get(GlobalMapping, mapping_id)
        assert mapping is not None
        assert mapping.tenant_value == "renamed.example"
        assert mapping.company_id == "COMPANY-2"
        assert mapping.description == "Updated mapping"


def test_tenantmap_group_create_edit_and_delete_are_atomic(client, app):
    """Manage several aliases as one row while retaining flat match records."""
    _authenticate(client)
    created = client.post(
        "/tenantmap/add",
        data={
            "tenant_values": "alpha.example\nalpha.onmicrosoft.com\n*.alpha.example\nalpha.example",
            "company_id": "ALPHA",
            "description": "Alpha group",
        },
    )
    assert created.status_code == 302

    with app.app_context():
        rows = GlobalMapping.query.order_by(GlobalMapping.tenant_value).all()
        assert [row.tenant_value for row in rows] == ["*.alpha.example", "alpha.example", "alpha.onmicrosoft.com"]
        assert len({row.mapping_group_id for row in rows}) == 1
        group_id = rows[0].mapping_group_id
        assert group_id is not None
        assert any(row.id == group_id for row in rows)

    page = client.get("/tenantmap").get_data(as_text=True)
    assert page.count('class="hw-tenantmap-row"') == 1
    assert "alpha.onmicrosoft.com" in page
    assert "*.alpha.example" in page

    updated = client.post(
        f"/tenantmap/edit/{group_id}",
        data={
            "tenant_values": "alpha.example\nnew.alpha.example",
            "company_id": "ALPHA-NEW",
            "description": "Updated group",
        },
    )
    assert updated.status_code == 302

    with app.app_context():
        rows = GlobalMapping.query.order_by(GlobalMapping.tenant_value).all()
        assert [row.tenant_value for row in rows] == ["alpha.example", "new.alpha.example"]
        assert {row.mapping_group_id for row in rows} == {group_id}
        assert {row.company_id for row in rows} == {"ALPHA-NEW"}
        assert {row.description for row in rows} == {"Updated group"}

    deleted = client.post(f"/tenantmap/delete/{group_id}")
    assert deleted.status_code == 302
    with app.app_context():
        assert GlobalMapping.query.count() == 0


def test_tenantmap_duplicate_alias_rejects_the_complete_group(client, app):
    """Do not partially create a group when any alias already belongs elsewhere."""
    _authenticate(client)
    first = client.post(
        "/tenantmap/add",
        data={"tenant_values": "shared.example", "company_id": "FIRST"},
    )
    second = client.post(
        "/tenantmap/add",
        data={"tenant_values": "free.example\nshared.example", "company_id": "SECOND"},
        follow_redirects=True,
    )

    assert first.status_code == 302
    assert second.status_code == 200
    assert b"Tenant value already mapped: shared.example." in second.data
    with app.app_context():
        assert [row.tenant_value for row in GlobalMapping.query.all()] == ["shared.example"]


def test_tenantmap_worker_cache_refreshes_when_revision_changes(client, app):
    """Refresh worker aliases immediately after the Redis revision advances."""
    from hookwise import tasks

    with app.app_context(), patch.object(tasks.redis_client, "get", side_effect=[b"1", b"1", b"2"]):
        tasks._cached_mappings = None
        tasks._cached_mapping_revision = None
        tasks._last_cache_update = 0.0
        db.session.add(GlobalMapping(tenant_value="first.example", company_id="FIRST"))
        db.session.commit()
        assert len(tasks.get_all_global_mappings()) == 1

        db.session.add(GlobalMapping(tenant_value="second.example", company_id="SECOND"))
        db.session.commit()
        assert len(tasks.get_all_global_mappings()) == 1
        assert len(tasks.get_all_global_mappings()) == 2
