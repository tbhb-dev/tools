"""Refreshed credentials remain a constrained JSON data object."""

import json

import pytest

from tbhbdev.alpha.agent.decisions import auth


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        (None, {}, None),
        ({}, {}, None),
        ({}, {"new": "value"}, "auth.new: found string; unexpected key"),
        ([], {}, "auth.before: found array; expected object"),
        ({}, [], "auth.after: found array; expected object"),
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
            "last_refresh: found string; timestamp moved backwards",
        ),
        (
            {"last_refresh": None},
            {"last_refresh": "2026-01-01T00:00:00Z"},
            "last_refresh: found null; expected timestamp with timezone",
        ),
        (
            {"last_refresh": "invalid"},
            {"last_refresh": "2026-01-01T00:00:00Z"},
            "last_refresh: found string; expected timestamp with timezone",
        ),
        (
            {"last_refresh": "2026-01-01T00:00:00Z"},
            {"last_refresh": None},
            "last_refresh: found null; expected timestamp with timezone",
        ),
        (
            {"last_refresh": "2026-01-01T00:00:00Z"},
            {"last_refresh": "invalid"},
            "last_refresh: found string; expected timestamp with timezone",
        ),
        (
            {"last_refresh": "2026-01-01T00:00:00Z"},
            {"last_refresh": "2026-01-01"},
            "last_refresh: found string; expected timestamp with timezone",
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
    assert auth.validate(before, after) == "auth: found invalid JSON"


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("OPENAI_API_KEY", None, None),
        ("OPENAI_API_KEY", "fixture-key", None),
        (
            "OPENAI_API_KEY",
            1,
            "auth.OPENAI_API_KEY: found number; expected optional string",
        ),
        ("last_refresh", "2026-01-01T00:00:00Z", None),
        (
            "last_refresh",
            None,
            "last_refresh: found null; expected timestamp with timezone",
        ),
        ("auth_mode", "fixture-mode", "auth.auth_mode: found string; unexpected key"),
    ],
)
def test_refresh_can_add_only_codex_serialized_fields(
    key: str, value: auth.Json, reason: str | None
) -> None:
    assert auth.validate("{}", json.dumps({key: value})) == reason


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


TOKENS = {
    "id_token": "fixture-id",
    "access_token": "fixture-access",
    "refresh_token": "fixture-refresh",
}


@pytest.mark.parametrize("key", ["account_id", "login_owner_id", "oauth_provenance"])
@pytest.mark.parametrize("optional", [None, "absent"])
def test_optional_token_fields_accept_null_or_absence(
    key: str, optional: str | None
) -> None:
    tokens: dict[str, object] = dict(TOKENS)
    if optional is None:
        tokens[key] = None
    document = json.dumps({"tokens": tokens})
    assert auth.validate(document, document) is None


@pytest.mark.parametrize("key", ["account_id", "login_owner_id"])
def test_optional_token_strings(key: str) -> None:
    document = json.dumps({"tokens": TOKENS | {key: "fixture-optional"}})
    assert auth.validate(document, document) is None


@pytest.mark.parametrize(
    ("key", "value", "kind"),
    [
        (key, value, kind)
        for key in (
            "id_token",
            "access_token",
            "refresh_token",
            "account_id",
            "login_owner_id",
        )
        for value, kind in (
            ({"private": "never-log"}, "object"),
            (87654321, "number"),
            (["never-log"], "array"),
            (False, "boolean"),
        )
    ]
    + [(key, None, "null") for key in ("id_token", "access_token", "refresh_token")],
)
def test_wrong_token_type_names_key_and_json_type_only(
    key: str, value: object, kind: str
) -> None:
    reason = auth.validate(
        json.dumps({"tokens": TOKENS}), json.dumps({"tokens": TOKENS | {key: value}})
    )
    expected = (
        "optional string" if key in {"account_id", "login_owner_id"} else "string"
    )
    assert reason == f"tokens.{key}: found {kind}; expected {expected}"
    assert reason is not None
    assert "never-log" not in reason
    assert "87654321" not in reason


@pytest.mark.parametrize("key", list(TOKENS))
def test_missing_required_token_names_key(key: str) -> None:
    tokens = {name: value for name, value in TOKENS.items() if name != key}
    assert (
        auth.validate('{"tokens":{}}', json.dumps({"tokens": tokens}))
        == f"tokens.{key}: found missing; expected string"
    )


@pytest.mark.parametrize(
    ("tokens", "kind"),
    [
        (None, "null"),
        ([], "array"),
        ("never-log", "string"),
        (87654321, "number"),
        (True, "boolean"),
    ],
)
def test_non_object_tokens(tokens: object, kind: str) -> None:
    assert (
        auth.validate('{"tokens":{}}', json.dumps({"tokens": tokens}))
        == f"tokens: found {kind}; expected object"
    )


def test_missing_tokens() -> None:
    assert (
        auth.validate('{"tokens":{}}', "{}") == "tokens: found missing; expected object"
    )


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ({"issuer": "fixture", "client_id": "fixture"}, None),
        (
            {"issuer": "fixture"},
            "tokens.oauth_provenance.client_id: found missing; expected string",
        ),
        (
            {"client_id": "fixture"},
            "tokens.oauth_provenance.issuer: found missing; expected string",
        ),
        (
            {"issuer": "fixture", "client_id": "fixture", "extra": "never-log"},
            "tokens.oauth_provenance.extra: found string; unexpected key",
        ),
        (
            {"issuer": {"private": "never-log"}, "client_id": "fixture"},
            "tokens.oauth_provenance.issuer: found object; expected string",
        ),
        (
            {"issuer": "fixture", "client_id": 87654321},
            "tokens.oauth_provenance.client_id: found number; expected string",
        ),
        ("never-log", "tokens.oauth_provenance: found string; expected object"),
        ([], "tokens.oauth_provenance: found array; expected object"),
    ],
)
def test_oauth_provenance_schema(value: object, reason: str | None) -> None:
    assert (
        auth.validate(
            '{"tokens":{}}',
            json.dumps({"tokens": TOKENS | {"oauth_provenance": value}}),
        )
        == reason
    )


def test_unknown_token_key() -> None:
    assert (
        auth.validate(
            '{"tokens":{}}', json.dumps({"tokens": TOKENS | {"extra": "never-log"}})
        )
        == "tokens.extra: found string; unexpected key"
    )


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (0, "number"),
        (1.5, "number"),
        (True, "boolean"),
        (None, "null"),
        ({}, "object"),
        ([], "array"),
        ("fixture", "string"),
    ],
)
def test_json_type(value: auth.Json, kind: str) -> None:
    assert auth.json_type(value) == kind


def test_refresh_timestamp_without_prior_timestamp() -> None:
    assert (
        auth.validate(
            '{"last_refresh":"invalid"}', '{"last_refresh":"2026-01-01T00:00:00Z"}'
        )
        == "last_refresh: found string; expected timestamp with timezone"
    )
    assert auth.refresh_time({}, {"last_refresh": "2026-01-01T00:00:00Z"}) is None
