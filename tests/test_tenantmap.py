import re
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import GlobalMapping
from hookwise.services.tenant_mappings import TenantMappingUndoSnapshot


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
    snapshots = {}

    def store_snapshot(mapping, owner_id):
        token = f"undo-{len(snapshots) + 1}"
        snapshots[token] = (
            TenantMappingUndoSnapshot(
                original_mapping_id=mapping.id,
                tenant_values=mapping.tenant_values,
                company_id=mapping.company_id,
                description=mapping.description,
            ),
            owner_id,
        )
        return token

    def load_snapshot(token, owner_id):
        stored = snapshots.get(token)
        return stored[0] if stored and stored[1] == owner_id else None

    def discard_snapshot(token):
        snapshots.pop(token, None)

    with (
        patch("hookwise.tenantmap.bump_mapping_cache_revision"),
        patch("hookwise.tenantmap.store_mapping_undo_snapshot", side_effect=store_snapshot),
        patch("hookwise.tenantmap.load_mapping_undo_snapshot", side_effect=load_snapshot),
        patch("hookwise.tenantmap.discard_mapping_undo_snapshot", side_effect=discard_snapshot),
    ):
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
            "tenant_values": "alpha.example\nALPHA.ONMICROSOFT.COM.\n*.alpha.example\nalpha.example",
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
    rendered_aliases = set(re.findall(r'<code class="hw-tenantmap-alias">([^<]+)</code>', page))
    assert rendered_aliases == {"*.alpha.example", "alpha.example", "alpha.onmicrosoft.com"}

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


def test_tenantmap_delete_requires_confirmation_and_can_be_undone(client, app):
    """Keep deletion behind an explicit dialog and restore its complete group."""
    _authenticate(client)
    client.post(
        "/tenantmap/add",
        data={
            "tenant_values": "one.example\ntwo.example",
            "company_id": "UNDO-COMPANY",
            "description": "Undo fixture",
        },
    )
    with app.app_context():
        group_id = GlobalMapping.query.first().mapping_group_id

    page = client.get("/tenantmap").get_data(as_text=True)
    delete_form = re.search(
        rf'<form action="/tenantmap/delete/{group_id}".*?</form>',
        page,
        re.DOTALL,
    )
    assert delete_form is not None
    assert 'hx-boost="false"' in delete_form.group(0)
    assert 'type="button"' in delete_form.group(0)
    assert 'onclick="confirmMappingDelete(this)"' in delete_form.group(0)
    assert "form.addEventListener('submit'" not in page

    deleted = client.post(f"/tenantmap/delete/{group_id}", follow_redirects=True)
    assert deleted.status_code == 200
    assert b'id="tenantmap-undo-toast"' in deleted.data
    assert b"</svg>Undo" in deleted.data
    assert b'data-bs-delay="8000"' in deleted.data
    assert b"5 minutes" not in deleted.data
    with app.app_context():
        assert GlobalMapping.query.count() == 0

    restored = client.post("/tenantmap/delete/undo", follow_redirects=True)
    assert restored.status_code == 200
    assert b"Mapping with 2 tenant value(s) restored." in restored.data
    with app.app_context():
        rows = GlobalMapping.query.order_by(GlobalMapping.tenant_value).all()
        assert [row.tenant_value for row in rows] == ["one.example", "two.example"]
        assert {row.mapping_group_id for row in rows} == {group_id}
        assert db.session.get(GlobalMapping, group_id) is not None
        assert {row.company_id for row in rows} == {"UNDO-COMPANY"}
        assert {row.description for row in rows} == {"Undo fixture"}


def test_tenantmap_delete_fails_closed_when_undo_storage_is_unavailable(client, app):
    """Retain the mapping when the required undo snapshot cannot be persisted."""
    from hookwise.services.tenant_mappings import TenantMappingUndoUnavailable

    _authenticate(client)
    client.post(
        "/tenantmap/add",
        data={"tenant_values": "safe.example", "company_id": "SAFE"},
    )
    with app.app_context():
        group_id = GlobalMapping.query.one().mapping_group_id

    with patch(
        "hookwise.tenantmap.store_mapping_undo_snapshot",
        side_effect=TenantMappingUndoUnavailable("Redis unavailable"),
    ):
        response = client.post(f"/tenantmap/delete/{group_id}", follow_redirects=True)

    assert response.status_code == 200
    assert b"safe undo snapshot could not be created" in response.data
    with app.app_context():
        assert GlobalMapping.query.count() == 1


def test_tenantmap_undo_snapshot_is_short_lived_and_owner_bound():
    """Persist only an expiring opaque snapshot that another user cannot load."""
    from hookwise.services import tenant_mappings

    assert tenant_mappings.TENANT_MAPPING_UNDO_TTL_SECONDS == 15

    mapping = tenant_mappings.TenantMappingGroup(
        id="group-id",
        tenant_values=("one.example", "two.example"),
        company_id="COMPANY",
        description="Snapshot fixture",
    )
    with patch.object(tenant_mappings.redis_client, "set", return_value=True) as redis_set:
        token = tenant_mappings.store_mapping_undo_snapshot(mapping, "user:owner")

    redis_set.assert_called_once()
    _, payload = redis_set.call_args.args
    assert redis_set.call_args.kwargs == {
        "ex": tenant_mappings.TENANT_MAPPING_UNDO_TTL_SECONDS,
        "nx": True,
    }
    with patch.object(tenant_mappings.redis_client, "get", return_value=payload):
        restored = tenant_mappings.load_mapping_undo_snapshot(token, "user:owner")
        denied = tenant_mappings.load_mapping_undo_snapshot(token, "user:other")

    assert restored is not None
    assert restored.tenant_values == mapping.tenant_values
    assert restored.company_id == mapping.company_id
    assert restored.description == mapping.description
    assert denied is None


def test_tenantmap_duplicate_alias_rejects_the_complete_group(client, app):
    """Do not partially create a group when any alias already belongs elsewhere."""
    _authenticate(client)
    first = client.post(
        "/tenantmap/add",
        data={"tenant_values": "shared.example", "company_id": "FIRST"},
    )
    second = client.post(
        "/tenantmap/add",
        data={"tenant_values": "free.example\nSHARED.EXAMPLE.", "company_id": "SECOND"},
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
