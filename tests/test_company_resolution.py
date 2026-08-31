import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import CidMapping
from hookwise.services.company_resolution import CompanyResolutionError, observe_cid, resolve_company_identifier


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def test_direct_company_identifier_passes_through_without_cid_mapping():
    assert resolve_company_identifier("eworx", "23243", None) == "eworx"


def test_observed_cid_is_recorded_and_resolves_current_mapping(app):
    with app.app_context():
        row = observe_cid("23243", "eworxRO")
        assert row is not None
        assert row.customer_name == "eworxRO"
        assert row.seen_count == 1
        row.company_id = "eworx"
        assert resolve_company_identifier("23243", "23243", row) == "eworx"


def test_unknown_cid_has_actionable_error(app):
    with app.app_context():
        row = CidMapping(cid="23243", customer_name="eworxRO")
        with pytest.raises(CompanyResolutionError, match="CID 23243.*CIDMap"):
            resolve_company_identifier("23243", "23243", row)
