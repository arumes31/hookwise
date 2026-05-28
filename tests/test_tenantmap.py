import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import GlobalMapping, User


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    return app

@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        # Create a default admin user for auth session simulation
        admin = User(username="admin", password_hash="dummy", role="admin")
        db.session.add(admin)
        db.session.commit()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def login(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "test-uuid"
        sess["username"] = "admin"
        sess["role"] = "admin"

def test_tenantmap_list(client, app):
    login(client)
    with app.app_context():
        m1 = GlobalMapping(tenant_value="tenant1", company_id="comp1")
        db.session.add(m1)
        db.session.commit()

    resp = client.get("/tenantmap")
    assert resp.status_code == 200
    assert b"tenant1" in resp.data
    assert b"comp1" in resp.data

def test_add_mapping_success(client, app):
    login(client)
    resp = client.post("/tenantmap/add", data={
        "tenant_value": "new_tenant",
        "company_id": "new_comp",
        "description": "test description"
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b"Mapping for new_tenant added successfully" in resp.data

    with app.app_context():
        mapping = GlobalMapping.query.filter_by(tenant_value="new_tenant").first()
        assert mapping is not None
        assert mapping.company_id == "new_comp"
        assert mapping.description == "test description"

def test_add_mapping_missing_fields(client):
    login(client)
    resp = client.post("/tenantmap/add", data={
        "tenant_value": "",
        "company_id": "new_comp"
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b"Tenant Value and Company ID are required" in resp.data

def test_edit_mapping_success(client, app):
    login(client)
    with app.app_context():
        m = GlobalMapping(tenant_value="old_tenant", company_id="old_comp")
        db.session.add(m)
        db.session.commit()
        mapping_id = m.id

    resp = client.post(f"/tenantmap/edit/{mapping_id}", data={
        "tenant_value": "updated_tenant",
        "company_id": "updated_comp",
        "description": "updated desc"
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b"Mapping for updated_tenant updated successfully" in resp.data

    with app.app_context():
        mapping = GlobalMapping.query.get(mapping_id)
        assert mapping.tenant_value == "updated_tenant"
        assert mapping.company_id == "updated_comp"

def test_edit_mapping_not_found(client):
    login(client)
    resp = client.post("/tenantmap/edit/nonexistent-id", data={
        "tenant_value": "val",
        "company_id": "comp"
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b"Global mapping not found" in resp.data

def test_edit_mapping_missing_fields(client, app):
    login(client)
    with app.app_context():
        m = GlobalMapping(tenant_value="tenant", company_id="comp")
        db.session.add(m)
        db.session.commit()
        mapping_id = m.id

    resp = client.post(f"/tenantmap/edit/{mapping_id}", data={
        "tenant_value": "",
        "company_id": "comp"
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b"Tenant Value and Company ID are required" in resp.data

def test_delete_mapping_success(client, app):
    login(client)
    with app.app_context():
        m = GlobalMapping(tenant_value="to_delete", company_id="comp")
        db.session.add(m)
        db.session.commit()
        mapping_id = m.id

    resp = client.post(f"/tenantmap/delete/{mapping_id}", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Mapping for to_delete deleted" in resp.data

    with app.app_context():
        assert GlobalMapping.query.get(mapping_id) is None

def test_delete_mapping_not_found(client):
    login(client)
    resp = client.post("/tenantmap/delete/nonexistent-id", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Global mapping not found" in resp.data
