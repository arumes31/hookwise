import re

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import GlobalMapping


@pytest.fixture
def app():
    return create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def _authenticate(client):
    with client.session_transaction() as session:
        session["user_id"] = "tenantmap-user"
        session["username"] = "admin"
        session["role"] = "admin"


def test_tenantmap_forms_use_interactive_bootstrap_modal_structure(client):
    _authenticate(client)

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
    assert 'label for="add_tenant_value"' in html
    assert 'label for="edit_tenant_value"' in html


def test_content_modals_are_mounted_above_the_body_level_backdrop():
    with open("static/js/ux.js", encoding="utf-8") as ux_script:
        source = ux_script.read()

    assert "mountContentModals();" in source
    assert "document.querySelectorAll('#main-content .modal')" in source
    assert "document.body.appendChild(modal)" in source


def test_tenantmap_create_and_edit_persist(client, app):
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
