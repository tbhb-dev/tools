"""Connection settings accept known keys and reject mistyped values."""

from dataclasses import asdict

import pytest

from tbhbdev.analytics.decisions.config import parse
from tbhbdev.analytics.model import Settings

TEXT_KEYS = (
    "host",
    "database",
    "user",
    "password_file",
    "owner_role",
    "loader_role",
    "reader_role",
    "grafana_role",
)


def test_defaults() -> None:
    assert parse({}) == Settings(
        host="localhost",
        port=5432,
        database="analytics",
        user="analytics_owner",
        password_file=None,
        owner_role="analytics_owner",
        loader_role="analytics_loader",
        reader_role="analytics_reader",
        grafana_role="grafana_reader",
    )


@pytest.mark.parametrize("key", TEXT_KEYS)
def test_each_string_override(key: str) -> None:
    assert asdict(parse({key: "custom-value"}))[key] == "custom-value"


@pytest.mark.parametrize("key", TEXT_KEYS)
@pytest.mark.parametrize("value", [1, True, [], {}, "", " \t\n"])
def test_invalid_string_names_key(key: str, value: object) -> None:
    with pytest.raises(ValueError, match="analytics") as caught:
        parse({key: value})
    assert str(caught.value) == f"[analytics] {key} must be a non-blank string"


@pytest.mark.parametrize("key", TEXT_KEYS)
def test_none_is_only_valid_for_password_file(key: str) -> None:
    if key == "password_file":
        assert parse({key: None}).password_file is None
    else:
        with pytest.raises(ValueError, match=key):
            parse({key: None})


@pytest.mark.parametrize("port", [1, 5433, 65535])
def test_valid_ports(port: int) -> None:
    assert parse({"port": port}).port == port


@pytest.mark.parametrize("value", [None, True, False, "5432", 1.5, 0, -1, 65536])
def test_invalid_ports(value: object) -> None:
    with pytest.raises(ValueError, match="analytics") as caught:
        parse({"port": value})
    assert str(caught.value) == "[analytics] port must be an integer from 1 to 65535"


@pytest.mark.parametrize("key", ["unknown", "password", "roles"])
def test_unknown_keys(key: str) -> None:
    with pytest.raises(ValueError, match="analytics") as caught:
        parse({key: "example"})
    assert str(caught.value) == f"[analytics] {key} not recognized"


def test_parse_does_not_change_the_mapping() -> None:
    table: dict[str, object] = {"host": "example-db", "password_file": "password.txt"}
    original = dict(table)
    assert parse(table).password_file == table["password_file"]
    assert table == original


def test_default_role_names() -> None:
    settings = parse({})
    assert (
        settings.owner_role,
        settings.loader_role,
        settings.reader_role,
        settings.grafana_role,
    ) == ("analytics_owner", "analytics_loader", "analytics_reader", "grafana_reader")
