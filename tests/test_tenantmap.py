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
    with (
        patch("hookwise.tasks.redis_client") as mock_tasks_redis,
        patch("hookwise.api.redis_client") as mock_api_redis,
        patch("hookwise.extensions.redis_client") as mock_ext_redis,
    ):
        mock_tasks_redis.get.return_value = None
        mock_api_redis.get.return_value = None
        mock_ext_redis.get.return_value = None
        yield mock_tasks_redis, mock_api_redis, mock_ext_redis


@pytest.fixture
def auth_client(client, app):
    with app.app_context():
        u = User(username="admin", password_hash=generate_password_hash("password"), role="admin")
        db.session.add(u)
        db.session.commit()

        # Log in
        with client.session_transaction() as sess:
            sess["user_id"] = u.id
            sess["username"] = u.username
            sess["role"] = u.role

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
    assert b"comp1" in resp.data
    assert b"tenant2" in resp.data
    assert b"comp2" in resp.data


def test_add_mapping_success(auth_client, app):
    resp = auth_client.post(
        "/tenantmap/add",
        data={"tenant_value": "new_tenant", "company_id": "new_comp", "description": "new_desc"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"Mapping for new_tenant added successfully." in resp.data

    with app.app_context():
        mapping = GlobalMapping.query.filter_by(tenant_value="new_tenant").first()
        assert mapping is not None
        assert mapping.company_id == "new_comp"
        assert mapping.description == "new_desc"


def test_add_mapping_missing_fields(auth_client, app):
    resp = auth_client.post(
        "/tenantmap/add", data={"tenant_value": "", "company_id": "new_comp"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert b"Tenant Value and Company ID are required." in resp.data


def test_edit_mapping_success(auth_client, app):
    with app.app_context():
        m = GlobalMapping(tenant_value="old_tenant", company_id="old_comp")
        db.session.add(m)
        db.session.commit()
        mapping_id = m.id

    resp = auth_client.post(
        f"/tenantmap/edit/{mapping_id}",
        data={"tenant_value": "updated_tenant", "company_id": "updated_comp", "description": "updated_desc"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"Mapping for updated_tenant updated successfully." in resp.data

    with app.app_context():
        mapping = GlobalMapping.query.get(mapping_id)
        assert mapping.tenant_value == "updated_tenant"
        assert mapping.company_id == "updated_comp"


def test_edit_mapping_not_found(auth_client):
    resp = auth_client.post(
        "/tenantmap/edit/nonexistent-id", data={"tenant_value": "val", "company_id": "comp"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert b"Global mapping not found." in resp.data


def test_edit_mapping_missing_fields(auth_client, app):
    with app.app_context():
        m = GlobalMapping(tenant_value="test_tenant", company_id="test_comp")
        db.session.add(m)
        db.session.commit()
        mapping_id = m.id

    resp = auth_client.post(
        f"/tenantmap/edit/{mapping_id}", data={"tenant_value": "", "company_id": "updated_comp"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert b"Tenant Value and Company ID are required." in resp.data


def test_delete_mapping_success(auth_client, app):
    with app.app_context():
        m = GlobalMapping(tenant_value="to_delete", company_id="comp")
        db.session.add(m)
        db.session.commit()
        mapping_id = m.id

    resp = auth_client.post(f"/tenantmap/delete/{mapping_id}", follow_redirects=True)

    assert resp.status_code == 200
    assert b"Mapping for to_delete deleted." in resp.data

    with app.app_context():
        mapping = GlobalMapping.query.get(mapping_id)
        assert mapping is None


def test_delete_mapping_not_found(auth_client):
    resp = auth_client.post("/tenantmap/delete/nonexistent-id", follow_redirects=True)

    assert resp.status_code == 200
    assert b"Global mapping not found." in resp.data
