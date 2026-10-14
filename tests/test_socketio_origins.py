"""The live activity connection must work from the address the console is served on."""

import pytest

from hookwise.extensions import socketio_allowed_origins


@pytest.mark.parametrize("raw", [None, "", "   ", " , "])
def test_unset_origins_mean_same_origin(raw: str | None) -> None:
    """Unset or blank means same-origin (None)."""
    # None lets python-engineio accept the page's own origin, including the
    # public host behind a reverse proxy; a fixed localhost list rejected it.
    assert socketio_allowed_origins(raw) is None


def test_explicit_origins_are_kept() -> None:
    """A comma-separated list is passed through, trimmed."""
    assert socketio_allowed_origins("https://a.example, https://b.example") == [
        "https://a.example",
        "https://b.example",
    ]


def test_wildcard_is_kept() -> None:
    """'*' on its own allows every origin."""
    assert socketio_allowed_origins(" * ") == "*"


def test_wildcard_mixed_with_origins_is_rejected() -> None:
    """'*' combined with specific origins is a configuration error."""
    # python-engineio would read this as "allow every origin".
    with pytest.raises(ValueError, match="on its own"):
        socketio_allowed_origins("https://a.example, *")
