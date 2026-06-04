from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import GlobalMapping


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
def authenticated_client(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "test-user-id"
        sess["username"] = "testuser"
    return client


def test_tenantmap_list(authenticated_client, app):
    with app.app_context():
        m1 = GlobalMapping(tenant_value="tenant1", company_id="comp1")
        db.session.add(m1)
        db.session.commit()

    resp = authenticated_client.get("/tenantmap")
    assert resp.status_code == 200
    assert b"tenant1" in resp.data
    assert b"comp1" in resp.data


def test_add_mapping_success(authenticated_client, app):
    resp = authenticated_client.post(
        "/tenantmap/add",
        data={"tenant_value": "new_tenant", "company_id": "new_comp", "description": "test desc"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"Mapping for new_tenant added successfully." in resp.data

    with app.app_context():
        mapping = GlobalMapping.query.filter_by(tenant_value="new_tenant").first()
        assert mapping is not None
        assert mapping.company_id == "new_comp"


def test_add_mapping_missing_fields(authenticated_client):
    resp = authenticated_client.post(
        "/tenantmap/add", data={"tenant_value": "", "company_id": "new_comp"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert b"Tenant Value and Company ID are required." in resp.data


def test_add_mapping_error(authenticated_client, app):
    with patch("hookwise.extensions.db.session.commit") as mock_commit:
        mock_commit.side_effect = Exception("DB Error")
        resp = authenticated_client.post(
            "/tenantmap/add", data={"tenant_value": "err_tenant", "company_id": "err_comp"}, follow_redirects=True
        )

    assert resp.status_code == 200
    assert b"Error adding mapping: DB Error" in resp.data


def test_edit_mapping_success(authenticated_client, app):
    with app.app_context():
        m = GlobalMapping(tenant_value="old_tenant", company_id="old_comp")
        db.session.add(m)
        db.session.commit()
        m_id = m.id

    resp = authenticated_client.post(
        f"/tenantmap/edit/{m_id}",
        data={"tenant_value": "updated_tenant", "company_id": "updated_comp", "description": "updated desc"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"Mapping for updated_tenant updated successfully." in resp.data

    with app.app_context():
        mapping = GlobalMapping.query.get(m_id)
        assert mapping.tenant_value == "updated_tenant"
        assert mapping.company_id == "updated_comp"


def test_edit_mapping_not_found(authenticated_client):
    resp = authenticated_client.post(
        "/tenantmap/edit/nonexistent", data={"tenant_value": "val", "company_id": "comp"}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert b"Global mapping not found." in resp.data


def test_edit_mapping_error(authenticated_client, app):
    with app.app_context():
        m = GlobalMapping(tenant_value="err_edit", company_id="comp")
        db.session.add(m)
        db.session.commit()
        m_id = m.id

    with patch("hookwise.extensions.db.session.commit") as mock_commit:
        mock_commit.side_effect = Exception("Commit failed")
        resp = authenticated_client.post(
            f"/tenantmap/edit/{m_id}", data={"tenant_value": "new", "company_id": "new"}, follow_redirects=True
        )

    assert resp.status_code == 200
    assert b"Error updating mapping: Commit failed" in resp.data


def test_delete_mapping_success(authenticated_client, app):
    with app.app_context():
        m = GlobalMapping(tenant_value="to_delete", company_id="comp")
        db.session.add(m)
        db.session.commit()
        m_id = m.id

    resp = authenticated_client.post(f"/tenantmap/delete/{m_id}", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Mapping for to_delete deleted." in resp.data

    with app.app_context():
        assert GlobalMapping.query.get(m_id) is None


def test_delete_mapping_not_found(authenticated_client):
    resp = authenticated_client.post("/tenantmap/delete/nonexistent", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Global mapping not found." in resp.data


def test_delete_mapping_error(authenticated_client, app):
    """Test coverage for error path in delete_mapping."""
    with app.app_context():
        m = GlobalMapping(tenant_value="delete_fail", company_id="comp")
        db.session.add(m)
        db.session.commit()
        m_id = m.id

    with patch("hookwise.extensions.db.session.commit") as mock_commit:
        mock_commit.side_effect = Exception("Delete failed")
        resp = authenticated_client.post(f"/tenantmap/delete/{m_id}", follow_redirects=True)

    assert resp.status_code == 200
    assert b"Error deleting mapping: Delete failed" in resp.data

    # Verify it still exists because of rollback
    with app.app_context():
        assert GlobalMapping.query.get(m_id) is not None


def test_edit_mapping_missing_fields(authenticated_client, app):
    with app.app_context():
        m = GlobalMapping(tenant_value="edit_missing", company_id="comp")
        db.session.add(m)
        db.session.commit()
        m_id = m.id

    resp = authenticated_client.post(
        f"/tenantmap/edit/{m_id}", data={"tenant_value": "", "company_id": "new"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert b"Tenant Value and Company ID are required." in resp.data
