"""Refreshed credentials remain a constrained JSON data object."""

import json

import pytest

from tbhbdev.alpha.agent.decisions import auth


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        (None, {}, None),
        ({}, {}, None),
        ({}, {"new": "value"}, "auth added top-level keys"),
        ([], {}, "auth must be a JSON object"),
        ({}, [], "auth must be a JSON object"),
        ({"tokens": {}}, {"tokens": {}}, None),
        ({"tokens": {}}, {}, "tokens must be an object of strings"),
        ({"tokens": {}}, {"tokens": []}, "tokens must be an object of strings"),
        (
            {"tokens": {}},
            {"tokens": {"access": 3}},
            "tokens must be an object of strings",
        ),
        ({"tokens": {}}, {"tokens": {"access": "refreshed"}}, None),
        ({"last_refresh": "2026-01-01T00:00:00Z"}, {}, None),
        (
            {"last_refresh": "2026-01-01T00:00:00Z"},
            {"last_refresh": "2026-01-01T00:00:00+00:00"},
            None,
        ),
        (
            {"last_refresh": "2026-01-01T00:00:00Z"},
            {"last_refresh": "2026-01-02T00:00:00Z"},
            None,
        ),
        (
            {"last_refresh": "2026-01-01T00:00:00Z"},
            {"last_refresh": "2025-12-31T23:59:59Z"},
            "last_refresh moved backwards",
        ),
        (
            {"last_refresh": None},
            {"last_refresh": "2026-01-01T00:00:00Z"},
            "auth contains invalid JSON or timestamp",
        ),
        (
            {"last_refresh": "invalid"},
            {"last_refresh": "2026-01-01T00:00:00Z"},
            "auth contains invalid JSON or timestamp",
        ),
        (
            {"last_refresh": "2026-01-01T00:00:00Z"},
            {"last_refresh": None},
            "auth contains invalid JSON or timestamp",
        ),
        (
            {"last_refresh": "2026-01-01T00:00:00Z"},
            {"last_refresh": "invalid"},
            "auth contains invalid JSON or timestamp",
        ),
        (
            {"last_refresh": "2026-01-01T00:00:00Z"},
            {"last_refresh": "2026-01-01"},
            "auth contains invalid JSON or timestamp",
        ),
    ],
)
def test_auth_content(before: object, after: object, reason: str | None) -> None:
    assert (
        auth.validate(None if before is None else json.dumps(before), json.dumps(after))
        == reason
    )


@pytest.mark.parametrize(("before", "after"), [("invalid", "{}"), ("{}", "invalid")])
def test_invalid_json(before: str, after: str) -> None:
    assert auth.validate(before, after) == "auth contains invalid JSON or timestamp"


@pytest.mark.parametrize(
    ("value", "kind", "message"),
    [
        (None, TypeError, "last_refresh must be a timestamp"),
        ("2026-01-01", ValueError, "last_refresh must include a timezone"),
    ],
)
def test_timestamp_refusal_reason(
    value: object, kind: type[Exception], message: str
) -> None:
    with pytest.raises(kind) as caught:
        auth.timestamp(value)
    assert str(caught.value) == message
