import json

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import CidMapping
from hookwise.services.backups import export_backup, parse_backup, restore_backup


@pytest.fixture
def app():
    return create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = "admin"
            session["username"] = "admin"
            session["role"] = "admin"
        yield client
        db.session.remove()
        db.drop_all()


def test_cidmap_displays_detected_customer_and_assigns_company(client, app):
    with app.app_context():
        row = CidMapping(cid="23243", customer_name="eworxRO")
        db.session.add(row)
        db.session.commit()
        mapping_id = row.id

    response = client.get("/cidmap")
    assert response.status_code == 200
    assert b"23243" in response.data
    assert b"eworxRO" in response.data
    assert b"Unmapped" in response.data

    response = client.post(f"/cidmap/edit/{mapping_id}", data={"company_id": "eworx"})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(CidMapping, mapping_id).company_id == "eworx"


def test_cidmap_is_in_encrypted_backup_and_restore(app):
    with app.app_context():
        db.create_all()
        db.session.add(CidMapping(cid="23243", customer_name="eworxRO", company_id="eworx"))
        db.session.commit()
        document = parse_backup(export_backup())
        assert document["cid_mappings"][0]["cid"] == "23243"

        db.session.query(CidMapping).delete()
        db.session.commit()
        document["configs"] = []
        restore_backup(json.loads(json.dumps(document)))
        restored = CidMapping.query.filter_by(cid="23243").one()
        assert restored.customer_name == "eworxRO"
        assert restored.company_id == "eworx"
        db.session.remove()
        db.drop_all()
