from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash

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
        yield app.test_client()
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def mock_redis():
    with patch("hookwise.tasks.redis_client") as mock:
        mock.get.return_value = None
        yield mock


@pytest.fixture
def auth_client(client, app):
    with app.app_context():
        user = User(username="admin", password_hash=generate_password_hash("password"))
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        username = user.username

    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["username"] = username
    return client


def test_tenantmap_list(auth_client, app):
    with app.app_context():
        m1 = GlobalMapping(tenant_value="tenant1", company_id="comp1", description="desc1")
        m2 = GlobalMapping(tenant_value="tenant2", company_id="comp2")
        db.session.add_all([m1, m2])
        db.session.commit()

    resp = auth_client.get("/tenantmap")
    assert resp.status_code == 200
    assert b"tenant1" in resp.data
    assert b"tenant2" in resp.data
    assert b"comp1" in resp.data
    assert b"comp2" in resp.data


def test_add_mapping(auth_client, app):
    data = {"tenant_value": "new_tenant", "company_id": "new_comp", "description": "new_desc"}
    resp = auth_client.post("/tenantmap/add", data=data, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Mapping for new_tenant added successfully." in resp.data

    with app.app_context():
        mapping = GlobalMapping.query.filter_by(tenant_value="new_tenant").first()
        assert mapping is not None
        assert mapping.company_id == "new_comp"
        assert mapping.description == "new_desc"


def test_add_mapping_validation(auth_client, app):
    # Missing company_id
    data = {"tenant_value": "tenant_no_comp"}
    resp = auth_client.post("/tenantmap/add", data=data, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Tenant Value and Company ID are required." in resp.data

    with app.app_context():
        mapping = GlobalMapping.query.filter_by(tenant_value="tenant_no_comp").first()
        assert mapping is None


def test_edit_mapping(auth_client, app):
    with app.app_context():
        mapping = GlobalMapping(tenant_value="old_t", company_id="old_c")
        db.session.add(mapping)
        db.session.commit()
        mapping_id = mapping.id

    data = {"tenant_value": "updated_t", "company_id": "updated_c", "description": "updated_d"}
    resp = auth_client.post(f"/tenantmap/edit/{mapping_id}", data=data, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Mapping for updated_t updated successfully." in resp.data

    with app.app_context():
        updated = GlobalMapping.query.get(mapping_id)
        assert updated.tenant_value == "updated_t"
        assert updated.company_id == "updated_c"
        assert updated.description == "updated_d"


def test_delete_mapping(auth_client, app):
    with app.app_context():
        mapping = GlobalMapping(tenant_value="to_delete", company_id="comp_del")
        db.session.add(mapping)
        db.session.commit()
        mapping_id = mapping.id

    resp = auth_client.post(f"/tenantmap/delete/{mapping_id}", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Mapping for to_delete deleted." in resp.data

    with app.app_context():
        deleted = GlobalMapping.query.get(mapping_id)
        assert deleted is None


def test_tenantmap_auth(client):
    # GET /tenantmap
    resp = client.get("/tenantmap")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]

    # POST /tenantmap/add
    resp = client.post("/tenantmap/add")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
