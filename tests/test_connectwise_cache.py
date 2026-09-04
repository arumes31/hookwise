from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db


@pytest.fixture
def app():
    app = create_app(
        {
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        }
    )
    return app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        test_client = app.test_client()
        with test_client.session_transaction() as session:
            session["user_id"] = "test-user"
            session["username"] = "admin"
            session["role"] = "admin"
        yield test_client
        db.session.remove()
        db.drop_all()


@pytest.mark.parametrize(
    ("path", "client_method"),
    [
        ("/api/cw/statuses/7", "get_board_statuses"),
        ("/api/cw/types/7", "get_board_types"),
        ("/api/cw/subtypes/7", "get_board_subtypes"),
        ("/api/cw/items/7", "get_board_items"),
    ],
)
def test_empty_dependent_connectwise_lookups_are_not_cached(client, path, client_method):
    with (
        patch("hookwise.api.redis_client") as redis,
        patch(f"hookwise.api.cw_client.{client_method}", return_value=[]),
    ):
        redis.get.return_value = None

        response = client.get(path)

    assert response.status_code == 200
    assert response.json == []
    redis.set.assert_not_called()


@pytest.mark.parametrize(
    ("path", "client_method", "cache_key"),
    [
        ("/api/cw/statuses/7", "get_board_statuses", "hookwise_cw_statuses_7"),
        ("/api/cw/types/7", "get_board_types", "hookwise_cw_types_7"),
        ("/api/cw/subtypes/7", "get_board_subtypes", "hookwise_cw_subtypes_7"),
        ("/api/cw/items/7", "get_board_items", "hookwise_cw_items_7"),
    ],
)
def test_non_empty_dependent_connectwise_lookups_are_cached(client, path, client_method, cache_key):
    with (
        patch("hookwise.api.redis_client") as redis,
        patch(f"hookwise.api.cw_client.{client_method}", return_value=[{"id": 1, "name": "Value"}]),
    ):
        redis.get.return_value = None

        response = client.get(path)

    assert response.status_code == 200
    assert response.json == [{"id": 1, "name": "Value"}]
    redis.set.assert_called_once_with(cache_key, '[{"id": 1, "name": "Value"}]', ex=3600)


@pytest.mark.parametrize(
    ("path", "client_method", "cache_key", "ttl", "args", "kwargs"),
    [
        ("/api/cw/boards", "get_boards", "hookwise_cw_boards", 3600, (), {}),
        ("/api/cw/priorities", "get_priorities", "hookwise_cw_priorities", 86400, (), {}),
        ("/api/cw/statuses/7", "get_board_statuses", "hookwise_cw_statuses_7", 3600, (7,), {}),
        ("/api/cw/types/7", "get_board_types", "hookwise_cw_types_7", 3600, (7,), {}),
        ("/api/cw/subtypes/7", "get_board_subtypes", "hookwise_cw_subtypes_7", 3600, (7,), {}),
        ("/api/cw/items/7", "get_board_items", "hookwise_cw_items_7", 3600, (7,), {}),
        ("/api/cw/companies", "get_companies", "hookwise_cw_companies_default", 3600, (), {"search": None}),
    ],
)
def test_legacy_empty_connectwise_cache_entries_are_refreshed(
    client, path, client_method, cache_key, ttl, args, kwargs
):
    fresh = [{"id": 1, "name": "Value", "identifier": "VALUE"}]
    with (
        patch("hookwise.api.redis_client") as redis,
        patch(f"hookwise.api.cw_client.{client_method}", return_value=fresh) as provider,
    ):
        redis.get.return_value = b"[]"

        response = client.get(path)

    assert response.status_code == 200
    assert response.json == fresh
    provider.assert_called_once_with(*args, **kwargs)
    redis.set.assert_called_once_with(cache_key, '[{"id": 1, "name": "Value", "identifier": "VALUE"}]', ex=ttl)
